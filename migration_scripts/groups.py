"""Migrate user groups — previously only Frontegg Support could move them.

Groups are tenant-scoped: every read and write goes through the frontegg-tenant-id
header, so the step walks the tenants both accounts share (one groups listing per
side per tenant). Group ids are generated per environment and never portable, so
groups are matched by exact name within a tenant.

How it works:
  1. The environment-level groups configuration (enabled/rolesEnabled) is compared
     and, if it differs, the source body is replayed verbatim into the destination.
  2. Per common tenant, both sides are listed with _groupsRelations=rolesAndUsers,
     which returns each group's FULL role objects (id + key + tenantId) and member
     user objects in one call.
  3. Groups missing in the destination are created; existing ones are synced
     additively — roles and members present in the source but absent in the
     destination are added, and nothing is ever removed.
  4. Roles are translated by (tenant scope, key) via the shared role maps; group
     members are translated by email, so users must already exist in the
     destination (this step reports anyone who does not — it never creates users).

A duplicate-name create returns 409 ER-01123 "Group already exists"; that is
treated as already-exists and the group is refetched so role/member sync still runs.
"""

import os

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, build_role_maps,
                                   get_all_users, index_users_by_email,
                                   map_role_id, paced_request)

logger = get_logger()

GROUPS_PATH = "/identity/resources/groups/v1"
CONFIG_PATH = "/identity/resources/groups/v1/config"

# Fields of a source group that a create accepts; None values are omitted.
# `metadata` arrives as a string (or null) and is passed through as-is.
CREATE_FIELDS = ("name", "description", "color", "metadata")


def _tenant_id_of(tenant):
    if not isinstance(tenant, dict):
        return None
    return tenant.get("tenantId") or tenant.get("id")


def _migrate_config(source_client, destination_client, interval):
    """Replay the environment groups configuration if it differs."""
    logger.info("Checking environment groups configuration...")
    response = paced_request(
        source_client, "GET", f"{source_client.base_url}{CONFIG_PATH}",
        min_interval=interval, allow_status=(404,))
    if response.status_code == 404:
        log_warning("  Groups config endpoint not available on source; skipping config.")
        return
    source_config = response.json()

    response = paced_request(
        destination_client, "GET", f"{destination_client.base_url}{CONFIG_PATH}",
        min_interval=interval, allow_status=(404,))
    dest_config = response.json() if response.status_code != 404 else {}

    if isinstance(dest_config, dict) and isinstance(source_config, dict) and \
            all(dest_config.get(k) == v for k, v in source_config.items()):
        log_success("  Groups configuration already matches source.")
        return

    paced_request(
        destination_client, "POST", f"{destination_client.base_url}{CONFIG_PATH}",
        json_data=source_config, min_interval=interval)
    log_success(f"  Groups configuration updated: {source_config}")


def _get_groups(client, tenant_id, interval):
    """Every group in one tenant, with full role objects and member users."""
    response = paced_request(
        client, "GET", f"{client.base_url}{GROUPS_PATH}",
        headers={"frontegg-tenant-id": tenant_id},
        params={"_groupsRelations": "rolesAndUsers"},
        min_interval=interval, allow_status=(404,))
    if response.status_code == 404:
        return []
    payload = response.json()
    if isinstance(payload, list):
        return payload
    return payload.get("groups", []) if isinstance(payload, dict) else []


def _create_group(destination_client, tenant_id, group, interval):
    """Create one group; returns the created group, or None on 409 (already exists)."""
    body = {k: group.get(k) for k in CREATE_FIELDS if group.get(k) is not None}
    response = paced_request(
        destination_client, "POST", f"{destination_client.base_url}{GROUPS_PATH}",
        headers={"frontegg-tenant-id": tenant_id},
        json_data=body, min_interval=interval, allow_status=(409,))
    if response.status_code == 409:
        return None
    return response.json()


def migrate_groups(source_client, destination_client):
    log_section("User Groups")

    from migration_scripts.tenants import get_tenants

    interval = float(os.getenv("GROUPS_MIN_INTERVAL_SECONDS", INTERVAL_GENERAL))

    try:
        _migrate_config(source_client, destination_client, interval)
    except Exception as e:
        log_error(f"  Groups configuration migration failed: {e}")

    source_tenants = get_tenants(source_client) or []
    source_ids = [t for t in (_tenant_id_of(x) for x in source_tenants) if t]
    if not source_ids:
        log_warning("No tenants found in the source account; nothing to do.")
        return

    dest_tenants = get_tenants(destination_client) or []
    dest_ids = {t for t in (_tenant_id_of(x) for x in dest_tenants) if t}

    common = [t for t in source_ids if t in dest_ids]
    missing_tenants = [t for t in source_ids if t not in dest_ids]
    if missing_tenants:
        log_warning(f"{len(missing_tenants)} source tenants do not exist in the "
                    "destination and were skipped (run MIGRATE_TENANTS first):")
        for tenant_id in missing_tenants[:10]:
            logger.info(f"    {tenant_id}")

    role_maps = build_role_maps(source_client, destination_client)
    id_map = role_maps["id_map"]
    dest_by_scope_key = role_maps["dest_by_scope_key"]

    logger.info("Fetching users from destination account...")
    dest_by_email = index_users_by_email(get_all_users(destination_client))
    logger.info(f"  {len(dest_by_email)} destination users indexed by email")

    created = roles_added = users_added = in_sync = failed = 0
    unmapped_roles, absent_members, no_email_members, externally_managed = [], [], [], []

    logger.info(f"Syncing groups across {len(common)} common tenants "
                "(2 listings per tenant)...")

    for tenant_id in common:
        try:
            source_groups = _get_groups(source_client, tenant_id, interval)
        except Exception as e:
            log_error(f"  {tenant_id}: could not list source groups: {e}")
            failed += 1
            continue
        if not source_groups:
            continue

        try:
            dest_groups = _get_groups(destination_client, tenant_id, interval)
        except Exception as e:
            log_error(f"  {tenant_id}: could not list destination groups: {e}")
            failed += 1
            continue
        dest_by_name = {g.get("name"): g for g in dest_groups if g.get("name")}

        for group in source_groups:
            name = group.get("name")
            if not name:
                log_warning(f"  {tenant_id}: skipping a source group without a name "
                            f"(id={group.get('id')})")
                continue

            managed_by = group.get("managedBy")
            if managed_by and managed_by != "frontegg":
                externally_managed.append(f"{tenant_id} | {name} (managedBy={managed_by})")

            dest_group = dest_by_name.get(name)
            created_now = False
            if dest_group is None:
                try:
                    dest_group = _create_group(destination_client, tenant_id,
                                               group, interval)
                except Exception as e:
                    log_error(f"  {tenant_id}: failed to create group {name!r}: {e}")
                    failed += 1
                    continue
                if dest_group is None:
                    # 409 ER-01123 — created since our listing; refetch and sync it.
                    dest_groups = _get_groups(destination_client, tenant_id, interval)
                    dest_by_name = {g.get("name"): g for g in dest_groups
                                    if g.get("name")}
                    dest_group = dest_by_name.get(name)
                    if dest_group is None:
                        log_error(f"  {tenant_id}: group {name!r} reported as existing "
                                  "but not found on refetch; skipping")
                        failed += 1
                        continue
                else:
                    created += 1
                    created_now = True
                    dest_by_name[name] = dest_group
                    log_success(f"  {tenant_id}: created group {name!r}")

            group_id = dest_group.get("id")

            # Roles: translate by (scope, key) from the full role objects; fall
            # back to the id map. Additive — never remove destination roles.
            wanted_roles = set()
            for role in group.get("roles") or []:
                if not isinstance(role, dict):
                    continue
                dest_role_id = (map_role_id(role, dest_by_scope_key)
                                or id_map.get(role.get("id")))
                if dest_role_id:
                    wanted_roles.add(dest_role_id)
                else:
                    unmapped_roles.append(
                        f"{tenant_id} | {name} — {role.get('key') or role.get('id')}")
            have_roles = {r.get("id") for r in (dest_group.get("roles") or [])
                          if isinstance(r, dict)}
            add_roles = sorted(wanted_roles - have_roles)

            # Members: translate by email. Users are migrated by Frontegg Support,
            # so anyone absent in the destination is reported, not created.
            wanted_users = set()
            for member in group.get("users") or []:
                if not isinstance(member, dict):
                    continue
                email = (member.get("email") or "").strip().lower()
                if not email:
                    no_email_members.append(
                        f"{tenant_id} | {name} — user id {member.get('id')}")
                    continue
                dest_user = dest_by_email.get(email)
                if dest_user and dest_user.get("id"):
                    wanted_users.add(dest_user["id"])
                else:
                    absent_members.append(f"{tenant_id} | {name} — {email}")
            have_users = {u.get("id") for u in (dest_group.get("users") or [])
                          if isinstance(u, dict)}
            add_users = sorted(wanted_users - have_users)

            try:
                if add_roles:
                    paced_request(
                        destination_client, "POST",
                        f"{destination_client.base_url}{GROUPS_PATH}/{group_id}/roles",
                        headers={"frontegg-tenant-id": tenant_id},
                        json_data={"roleIds": add_roles}, min_interval=interval)
                    roles_added += len(add_roles)
                    log_success(f"  {tenant_id}: {name!r} — added {len(add_roles)} role(s)")
                if add_users:
                    paced_request(
                        destination_client, "POST",
                        f"{destination_client.base_url}{GROUPS_PATH}/{group_id}/users",
                        headers={"frontegg-tenant-id": tenant_id},
                        json_data={"userIds": add_users}, min_interval=interval)
                    users_added += len(add_users)
                    log_success(f"  {tenant_id}: {name!r} — added {len(add_users)} member(s)")
            except Exception as e:
                log_error(f"  {tenant_id}: failed to sync group {name!r}: {e}")
                failed += 1
                continue

            if not created_now and not add_roles and not add_users:
                in_sync += 1

    if unmapped_roles:
        log_warning(f"{len(unmapped_roles)} group roles have no destination equivalent "
                    "(run MIGRATE_ROLES first if that is unexpected):")
        for line in unmapped_roles[:10]:
            logger.info(f"    {line}")
    if absent_members:
        log_warning(f"{len(absent_members)} group members do not exist in the destination "
                    "and were skipped (migrate users first, then re-run this step):")
        for line in absent_members[:10]:
            logger.info(f"    {line}")
    if no_email_members:
        log_warning(f"{len(no_email_members)} group members carry no email and were skipped:")
        for line in no_email_members[:10]:
            logger.info(f"    {line}")
    if externally_managed:
        log_warning(f"{len(externally_managed)} groups are not managed by Frontegg (e.g. "
                    "SCIM); they were migrated but their manager may recreate or fight them:")
        for line in externally_managed[:10]:
            logger.info(f"    {line}")

    logger.print_stats("Groups Migration Summary", {
        "Groups created": created,
        "Roles added": roles_added,
        "Members added": users_added,
        "Groups already in sync": in_sync,
        "Tenants missing in destination": len(missing_tenants),
        "Failures": failed,
    })
    if failed:
        log_error(f"{failed} group operations failed — see errors above; the step is "
                  "idempotent, so re-running will retry only what is still missing.")
    else:
        log_success("Group migration completed.")
