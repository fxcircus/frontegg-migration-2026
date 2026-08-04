"""Migrate tenant (account) hierarchy — parent/child relationships.

Tenants are migrated preserving their original tenantIds, so hierarchy edges can
be replayed verbatim: create every tenant flat first (MIGRATE_TENANTS), then link
them here. That two-phase order is deliberate — the create-tenant endpoint also
accepts parentTenantId, but using it would require topologically sorting tenants
parent-first and would not be re-runnable.

Constraint worth knowing: there is no environment-wide endpoint that dumps every
parent->child edge. Every hierarchy read is scoped to a single tenant through the
frontegg-tenant-id header, so reading the source hierarchy costs one call per
tenant on top of the tenant listing.

Linking rejects an edge with 400 when the parameters fail validation, when parent
and child are the same tenant, or when the child already belongs to another
organisation's hierarchy.
"""

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import INTERVAL_TENANTS, paced_request

logger = get_logger()

HIERARCHY_PATH = "/tenants/resources/hierarchy/v1"

# Keys Frontegg uses for nested children across the hierarchy/tree shapes.
CHILD_KEYS = ("subTenants", "children", "tenants", "items")


def _tenant_id_of(node):
    if not isinstance(node, dict):
        return None
    return node.get("tenantId") or node.get("id")


def _walk(node, parent_id, edges):
    """Collect (parent, child) edges from a hierarchy payload.

    The success shape is not published in the OpenAPI spec, so this tolerates
    both a flat list of children and a nested subtree.
    """
    if isinstance(node, list):
        for item in node:
            _walk(item, parent_id, edges)
        return

    if not isinstance(node, dict):
        return

    tenant_id = _tenant_id_of(node)
    if tenant_id and parent_id and tenant_id != parent_id:
        edges.add((parent_id, tenant_id))

    next_parent = tenant_id or parent_id
    for key in CHILD_KEYS:
        if key in node:
            _walk(node[key], next_parent, edges)


def _read_edges(client, tenant_ids):
    """Every parent->child edge visible from the given tenants."""
    edges = set()
    for tenant_id in tenant_ids:
        try:
            response = paced_request(
                client, "GET", f"{client.base_url}{HIERARCHY_PATH}",
                headers={"frontegg-tenant-id": tenant_id},
                min_interval=INTERVAL_TENANTS, allow_status=(400, 404),
            )
        except Exception as e:
            logger.debug(f"  hierarchy read failed for {tenant_id}: {e}")
            continue
        if response.status_code >= 400:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        _walk(payload, tenant_id, edges)
    return edges


def migrate_tenant_hierarchy(source_client, destination_client):
    log_section("Tenant Hierarchy")

    from migration_scripts.tenants import get_tenants

    source_tenants = get_tenants(source_client) or []
    source_ids = [t for t in (_tenant_id_of(x) for x in source_tenants) if t]
    if not source_ids:
        log_warning("No tenants found in the source account; nothing to link.")
        return

    dest_tenants = get_tenants(destination_client) or []
    dest_ids = {t for t in (_tenant_id_of(x) for x in dest_tenants) if t}

    logger.info(f"Reading hierarchy from {len(source_ids)} source tenants "
                "(one call per tenant — no bulk endpoint exists)...")
    source_edges = _read_edges(source_client, source_ids)
    if not source_edges:
        log_success("Source account has no tenant hierarchy — nothing to migrate.")
        return
    logger.info(f"  {len(source_edges)} parent->child edge(s) in source")

    logger.info("Reading existing hierarchy from destination...")
    dest_edges = _read_edges(destination_client, sorted(dest_ids))
    logger.info(f"  {len(dest_edges)} edge(s) already in destination")

    created = skipped = failed = 0
    for parent_id, child_id in sorted(source_edges):
        if (parent_id, child_id) in dest_edges:
            skipped += 1
            continue
        missing = [t for t in (parent_id, child_id) if t not in dest_ids]
        if missing:
            log_warning(f"  Skipping {parent_id} -> {child_id}: tenant(s) missing in "
                        f"destination {missing} (run MIGRATE_TENANTS first)")
            skipped += 1
            continue

        try:
            response = paced_request(
                destination_client, "POST",
                f"{destination_client.base_url}{HIERARCHY_PATH}",
                json_data={"parentTenantId": parent_id, "childTenantId": child_id},
                min_interval=INTERVAL_TENANTS, allow_status=(400,),
            )
            if response.status_code == 400:
                # Most often: the child already sits under another parent.
                failed += 1
                log_error(f"  {parent_id} -> {child_id}: [400] {response.text[:200]}")
            else:
                created += 1
                log_success(f"  linked {parent_id} -> {child_id}")
        except Exception as e:
            failed += 1
            log_error(f"  {parent_id} -> {child_id}: {e}")

    log_success(f"Hierarchy edges created: {created}")
    if skipped:
        logger.info(f"Skipped (already linked or tenant missing): {skipped}")
    if failed:
        log_error(f"Failed: {failed}")
