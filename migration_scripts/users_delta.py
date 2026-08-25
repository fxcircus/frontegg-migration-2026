"""Create users that exist in the source account but not the destination.

OPT-IN cutover-delta step. Frontegg Support's user export migrates users
losslessly (password hashes and MFA secrets included), but any user who signed
up AFTER that export exists only in the source. This step closes that gap via
the data-migration endpoint (POST /identity/resources/migrations/v1/local).

*** LOUD CAVEAT ***
The public API cannot read password hashes or MFA secrets, so users created by
this step arrive WITHOUT a password and WITHOUT MFA enrollment. They must reset
their password (or sign in via SSO / passwordless) in the destination, and
re-enroll MFA. If that is unacceptable, ask Frontegg Support to run a delta
migration instead — that remains the only lossless route.

How it works:
  1. One sweep of /identity/resources/users/v2 per account; destination users
     are indexed by email, so only genuinely missing users are considered.
  2. Each missing user is created into ONE primary tenant (the source user's
     own tenantId when it exists in the destination, else the first source
     membership tenant that does). That tenant's roles are translated to
     destination role IDs by (tenant scope, key). Users whose tenants are all
     absent from the destination are skipped — migrate tenants first.
  3. Remaining tenant memberships are intentionally NOT created here; the
     MIGRATE_USER_TENANT_ROLES step (which runs after this one) fills them in.

Re-runnable: a second run finds no missing users and creates nothing; 409s
(user already exists) are counted, never fatal. The migration route is a
management endpoint, so the step paces at INTERVAL_GENERAL.
"""

import csv
import json
import os

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, build_role_maps,
                                   get_all_users, index_users_by_email,
                                   map_role_id, membership_role_objs,
                                   paced_request)

logger = get_logger()

MIGRATION_PATH = "/identity/resources/migrations/v1/local"
# Providers the migration endpoint accepts; anything else becomes "local".
KNOWN_PROVIDERS = ("local", "saml", "google", "github", "facebook", "microsoft",
                   "scim2", "slack", "apple")
DEFAULT_OUTPUT_CSV = "migrated_missing_users.csv"


def _existing_tenant_ids(client):
    """tenantIds present in the destination, so we never create into a missing one."""
    from migration_scripts.tenants import get_tenants
    tenants = get_tenants(client) or []
    ids = set()
    for tenant in tenants:
        tenant_id = tenant.get("tenantId") or tenant.get("id")
        if tenant_id:
            ids.add(tenant_id)
    return ids


def _stringified(value):
    """metadata/vendorMetadata for the migration body: string in, string out.

    Source values are usually already JSON strings — pass those through; dicts
    are dumped; empty/absent values return None so the field can be omitted.
    """
    if isinstance(value, dict):
        return json.dumps(value) if value else None
    if isinstance(value, str) and value.strip():
        return value
    return None


def _build_body(user, primary_tenant, role_ids):
    provider = (user.get("provider") or "").strip().lower()
    body = {
        "tenantId": primary_tenant,
        "email": user["email"],
        "provider": provider if provider in KNOWN_PROVIDERS else "local",
        "verifyUser": bool(user.get("verified")),
    }
    for field in ("name", "phoneNumber", "profilePictureUrl", "externalId", "username"):
        value = user.get(field)
        if isinstance(value, str) and value.strip():
            body[field] = value
    for field in ("metadata", "vendorMetadata"):
        value = _stringified(user.get(field))
        if value is not None:
            body[field] = value
    # Empty/omitted roleIds makes the environment assign its default roles, so
    # only pass the list when the source roles actually mapped to something.
    if role_ids:
        body["roleIds"] = role_ids
    return body


def migrate_missing_users(source_client, destination_client):
    log_section("Missing Users (Cutover Delta)")

    log_warning("Users created by this step have NO password and NO MFA enrollment "
                "(not readable via the public API). They must reset their password or "
                "sign in via SSO/passwordless. Frontegg Support's delta migration is "
                "the lossless alternative.")

    interval = float(os.getenv("USERS_DELTA_MIN_INTERVAL_SECONDS", INTERVAL_GENERAL))
    csv_path = os.getenv("MISSING_USERS_OUTPUT_CSV", DEFAULT_OUTPUT_CSV)

    logger.info("Fetching users from source account...")
    source_users = get_all_users(source_client)
    logger.info(f"  {len(source_users)} users in source")

    logger.info("Fetching users from destination account...")
    dest_users = get_all_users(destination_client)
    logger.info(f"  {len(dest_users)} users in destination")

    if not source_users:
        log_warning("No users in the source account; nothing to do.")
        return

    dest_by_email = index_users_by_email(dest_users)
    dest_tenant_ids = _existing_tenant_ids(destination_client)

    role_maps = build_role_maps(source_client, destination_client)
    role_map = role_maps["id_map"]
    dest_by_scope_key = role_maps["dest_by_scope_key"]
    if role_maps["unmapped"]:
        log_warning(f"{len(role_maps['unmapped'])} source roles have no destination "
                    "equivalent (run MIGRATE_ROLES first if that is unexpected).")

    missing = []
    for user in source_users:
        email = (user.get("email") or "").strip().lower()
        if email and email not in dest_by_email:
            missing.append(user)

    if not missing:
        log_success("No missing users — every source user already exists in the "
                    "destination.")
        return

    logger.info(f"{len(missing)} source users do not exist in the destination "
                f"(pacing {interval}s between creates)...")

    url = f"{destination_client.base_url}{MIGRATION_PATH}"
    created_rows = []
    already_existed, extra_memberships = [], 0
    skipped_no_tenant, unmappable_roles, failed = [], [], []

    for user in missing:
        email = (user.get("email") or "").strip().lower()
        memberships = membership_role_objs(user)
        if not memberships and user.get("tenantId"):
            # Some stacks omit tenants[] for single-tenant users; the user's own
            # tenantId is still a real membership (with no listed roles).
            memberships = {user["tenantId"]: []}

        candidates = [t for t in memberships if t in dest_tenant_ids]
        if not candidates:
            skipped_no_tenant.append(email)
            continue
        primary = user.get("tenantId") if user.get("tenantId") in candidates else candidates[0]
        if len(candidates) > 1:
            extra_memberships += 1

        # Translate the primary tenant's roles by (scope, key), falling back to
        # the id map. Unmappable roles are reported but never block the create.
        mapped, unmapped_here = [], []
        for role in memberships.get(primary) or []:
            dest_id = map_role_id(role, dest_by_scope_key) or role_map.get(role.get("id"))
            if dest_id:
                mapped.append(dest_id)
            else:
                unmapped_here.append(role.get("key") or role.get("id"))
        mapped = sorted(set(mapped))
        if unmapped_here:
            unmappable_roles.append((email, primary, unmapped_here))

        body = _build_body({**user, "email": email}, primary, mapped)
        try:
            response = paced_request(destination_client, "POST", url, json_data=body,
                                     min_interval=interval, allow_status=(400, 409))
        except Exception as e:
            failed.append((email, str(e)))
            log_error(f"  {email}: {e}")
            continue

        if response.status_code == 409:
            already_existed.append(email)
            logger.info(f"  {email}: already exists in destination [409]")
        elif response.status_code == 400:
            failed.append((email, response.text[:200]))
            log_error(f"  {email}: [400] {response.text[:200]}")
        else:
            try:
                dest_id = (response.json() or {}).get("id", "")
            except ValueError:
                dest_id = ""
            created_rows.append({
                "email": email,
                "primaryTenantId": primary,
                "destUserId": dest_id,
                "mappedRoleIds": ";".join(mapped),
                "sourceCreatedAt": user.get("createdAt") or "",
            })
            log_success(f"  Created {email} in tenant {primary} "
                        f"({len(mapped)} role(s))")

    if skipped_no_tenant:
        log_warning(f"{len(skipped_no_tenant)} users skipped because none of their "
                    "tenants exist in the destination (run MIGRATE_TENANTS first):")
        for email in skipped_no_tenant[:10]:
            logger.info(f"    {email}")
    if unmappable_roles:
        log_warning(f"{len(unmappable_roles)} users had roles with no destination "
                    "equivalent (created with the mappable subset only):")
        for email, tenant_id, keys in unmappable_roles[:10]:
            logger.info(f"    {tenant_id} | {email} — {keys}")
    if failed:
        log_warning(f"{len(failed)} users failed to create:")
        for email, reason in failed[:10]:
            logger.info(f"    {email} — {reason}")

    if created_rows:
        # Append across runs — each run's rows are an audit record of users this
        # tool created without passwords, so earlier rows must never be lost.
        fresh = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["email", "primaryTenantId",
                                                   "destUserId", "mappedRoleIds",
                                                   "sourceCreatedAt"])
            if fresh:
                writer.writeheader()
            writer.writerows(created_rows)
        log_success(f"Wrote {len(created_rows)} created user(s) to {csv_path}")

    log_success(f"Users created: {len(created_rows)}")
    if already_existed:
        logger.info(f"Already existed (409): {len(already_existed)}")
    if skipped_no_tenant:
        log_warning(f"Skipped (no destination tenant): {len(skipped_no_tenant)}")
    if failed:
        log_error(f"Failed: {len(failed)}")
    if extra_memberships:
        logger.info(f"{extra_memberships} created user(s) hold further source tenant "
                    "memberships — MIGRATE_USER_TENANT_ROLES (which runs after this "
                    "step) fills those in.")
    log_warning("Reminder: these users have no password/MFA — they must reset their "
                "password or use SSO/passwordless on first login.")
