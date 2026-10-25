"""Migrate user tenant memberships and their per-tenant roles.

This step does NOT create users. It assumes the users already exist in the
destination account (Frontegg Support migrates users, including password hashes
and MFA secrets, which are not readable through the public API). What it fixes is
the gap that remains afterwards: a user who belongs to several accounts (tenants)
in the source but only one in the destination, and the roles they held on each.

How it works:
  1. One sweep of /identity/resources/users/v2 per account returns every user with
     a `tenants` array of {tenantId, roles[]}. That array can omit memberships
     (accepted and pending invitations alike), so every tenant is also listed
     on its own and the memberships found only there are added back, with their
     roles read from /users/v3/roles (see complete_memberships).
  2. Source role IDs are translated to destination role IDs by (tenant scope, key).
  3. Missing memberships are grouped by tenant and sent as one bulk invite per
     tenant, with skipInviteEmail so nobody is emailed.

Bulk invite is limited to 5/min per environment on Launch (10/min on Scale and
Enterprise), scoped per environment rather than per IP, so parallelism cannot
speed it up. The step paces itself accordingly and backs off on 429.
"""

import os

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_BULK_INVITE, build_role_maps,
                                   complete_memberships, get_all_users,
                                   index_users_by_email, map_role_id,
                                   membership_role_objs, memberships_of,
                                   paced_request)

logger = get_logger()

INVITE_PATH = "/identity/resources/users/bulk/v1/invite"
# Frontegg accepts up to 1000 users per bulk invite; keep batches well under that.
MAX_USERS_PER_REQUEST = 200


def _existing_tenant_ids(client):
    """tenantIds present in the destination, so we never invite into a missing one."""
    from migration_scripts.tenants import get_tenants
    tenants = get_tenants(client) or []
    ids = set()
    for tenant in tenants:
        tenant_id = tenant.get("tenantId") or tenant.get("id")
        if tenant_id:
            ids.add(tenant_id)
    return ids


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def migrate_user_tenant_roles(source_client, destination_client):
    log_section("User Tenant Memberships & Roles")

    interval = float(os.getenv("BULK_INVITE_MIN_INTERVAL_SECONDS", INTERVAL_BULK_INVITE))

    logger.info("Fetching users from source account...")
    source_users = get_all_users(source_client)
    logger.info(f"  {len(source_users)} users in source")

    logger.info("Fetching users from destination account...")
    dest_users = get_all_users(destination_client)
    logger.info(f"  {len(dest_users)} users in destination")

    if not source_users:
        log_warning("No users in the source account; nothing to do.")
        return

    role_maps = build_role_maps(source_client, destination_client)
    role_map, unmapped_roles = role_maps["id_map"], role_maps["unmapped"]
    dest_by_scope_key = role_maps["dest_by_scope_key"]
    if unmapped_roles:
        log_warning(f"{len(unmapped_roles)} source roles have no destination equivalent "
                    "(run MIGRATE_ROLES first if that is unexpected):")
        for role in unmapped_roles[:10]:
            scope = role.get("tenantId") or "account-level"
            logger.info(f"    key={role.get('key')!r} scope={scope}")

    dest_by_email = index_users_by_email(dest_users)
    dest_tenant_ids = _existing_tenant_ids(destination_client)

    logger.info("Cross-checking memberships against the per-tenant user listings...")
    complete_memberships(source_client, source_users, sorted(_existing_tenant_ids(source_client)), "source")
    complete_memberships(destination_client, dest_users, sorted(dest_tenant_ids), "destination")

    plan = {}
    absent_users, missing_tenants, unmappable, role_drift = [], set(), [], []

    for user in source_users:
        email = (user.get("email") or "").strip().lower()
        if not email:
            continue
        dest_user = dest_by_email.get(email)
        if not dest_user:
            absent_users.append(email)
            continue

        source_memberships = membership_role_objs(user)
        dest_memberships = memberships_of(dest_user)

        for tenant_id, source_roles in source_memberships.items():
            if tenant_id not in dest_tenant_ids:
                missing_tenants.add(tenant_id)
                continue

            # Translate by (scope, key) from the role objects themselves; fall
            # back to the id map for stacks whose user listings omit role keys.
            mapped, unmapped_here = [], []
            for role in source_roles:
                dest_id = map_role_id(role, dest_by_scope_key) or role_map.get(role.get("id"))
                if dest_id:
                    mapped.append(dest_id)
                else:
                    unmapped_here.append(role.get("key") or role.get("id"))

            if tenant_id in dest_memberships:
                # Membership already exists. Report role drift rather than
                # re-inviting, which would not remove or replace roles anyway.
                if set(mapped) - set(dest_memberships[tenant_id]):
                    role_drift.append((email, tenant_id))
                continue

            if unmapped_here:
                unmappable.append((email, tenant_id, unmapped_here))
                continue
            if not mapped:
                # A membership can legitimately carry no roles; inviting with an
                # empty roleIds array is rejected, so flag it for manual handling.
                unmappable.append((email, tenant_id, ["<no roles on source tenant>"]))
                continue

            plan.setdefault(tenant_id, []).append({
                "email": email,
                "name": (user.get("name") or "").strip() or email,
                "provider": "local",
                "skipInviteEmail": True,
                "roleIds": sorted(set(mapped)),
                "verified": True,
            })

    if absent_users:
        log_warning(f"{len(absent_users)} source users do not exist in the destination "
                    "and were skipped (users are migrated by Frontegg Support):")
        for email in absent_users[:10]:
            logger.info(f"    {email}")
    if missing_tenants:
        log_warning(f"{len(missing_tenants)} tenants referenced by source memberships do not "
                    "exist in the destination (run MIGRATE_TENANTS first):")
        for tenant_id in sorted(missing_tenants)[:10]:
            logger.info(f"    {tenant_id}")
    if unmappable:
        log_warning(f"{len(unmappable)} memberships skipped because their roles could not "
                    "be mapped:")
        for email, tenant_id, keys in unmappable[:10]:
            logger.info(f"    {tenant_id} | {email} — {keys}")
    if role_drift:
        log_warning(f"{len(role_drift)} existing memberships have roles in the source that "
                    "are missing in the destination (not changed by this step):")
        for email, tenant_id in role_drift[:10]:
            logger.info(f"    {tenant_id} | {email}")

    total = sum(len(v) for v in plan.values())
    if not total:
        log_success("No missing tenant memberships — destination already matches source.")
        return

    logger.info(f"Creating {total} memberships across {len(plan)} tenants "
                f"(pacing {interval}s between requests)...")

    url = f"{destination_client.base_url}{INVITE_PATH}"
    created = failed = 0
    for tenant_id, users in sorted(plan.items()):
        for batch in _chunk(users, MAX_USERS_PER_REQUEST):
            try:
                response = paced_request(
                    destination_client, "POST", url,
                    headers={"frontegg-tenant-id": tenant_id},
                    json_data={"users": batch},
                    min_interval=interval,
                )
                # The endpoint is asynchronous: 202 + a job id means accepted.
                if response.status_code in (200, 201, 202):
                    created += len(batch)
                    log_success(f"  {tenant_id}: invited {len(batch)} user(s) "
                                f"[{response.status_code}]")
                else:
                    failed += len(batch)
                    log_error(f"  {tenant_id}: [{response.status_code}] {response.text[:200]}")
            except Exception as e:
                failed += len(batch)
                log_error(f"  {tenant_id}: {e}")

    log_success(f"Memberships submitted: {created}")
    if failed:
        log_error(f"Memberships failed: {failed}")
    log_warning("Bulk invite is asynchronous — re-run this step to confirm; it is "
                "idempotent and will report nothing left to do once complete.")
