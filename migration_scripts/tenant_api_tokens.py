"""Migrate tenant API tokens (client-credentials / M2M tokens).

IMPORTANT CAVEAT — credentials cannot be preserved.
Frontegg generates the clientId and the secret server-side and returns the secret
exactly once, in the create response. There is no field to supply an existing
clientId or secret, and no import endpoint anywhere in the public API. Every
migrated token therefore gets NEW credentials, and every consumer of the old
token (integrations, scripts, CI jobs) has to be re-pointed.

Because the secret is unrecoverable after creation, each new token is written to
a handover CSV the moment it is created, alongside the source clientId it
replaces so you can tell each customer exactly which credential to swap.

There is no account-wide listing endpoint, so tokens are enumerated per tenant.
"""

import csv
import os
from datetime import datetime, timezone

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, build_role_map,
                                   paced_request)

logger = get_logger()

LIST_PATH = "/identity/resources/tenants/api-tokens/v1"
# v1 create is deprecated ("Do not use. Instead, use v2 of this route.").
CREATE_PATH = "/identity/resources/tenants/api-tokens/v2"

OUTPUT_FIELDS = ["tenantId", "description", "newClientId", "newSecret",
                 "roleIds", "expiresInMinutes", "sourceClientId", "createdAt"]


def _output_path():
    return os.getenv("API_TOKENS_OUTPUT_CSV", "migrated_tenant_api_tokens.csv")


def _list_tokens(client, tenant_id):
    url = f"{client.base_url}{LIST_PATH}"
    response = paced_request(client, "GET", url,
                             headers={"frontegg-tenant-id": tenant_id},
                             min_interval=INTERVAL_GENERAL)
    payload = response.json()
    # The route returns a plain array, not a pagination wrapper.
    return payload if isinstance(payload, list) else payload.get("items", [])


def _minutes_until(expires):
    """Frontegg takes a relative TTL; the source reports an absolute timestamp."""
    if not expires:
        return None
    try:
        parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    minutes = int((parsed - datetime.now(timezone.utc)).total_seconds() // 60)
    return minutes if minutes >= 1 else None


def migrate_tenant_api_tokens(source_client, destination_client):
    log_section("Tenant API Tokens")
    log_warning("Migrated tokens receive NEW clientId and secret values — the originals "
                "cannot be carried over. Consumers must be re-pointed.")

    from migration_scripts.tenants import get_tenants

    include_expired = os.getenv("MIGRATE_EXPIRED_API_TOKENS", "False").lower() == "true"

    tenants = get_tenants(source_client) or []
    tenant_ids = [t.get("tenantId") or t.get("id") for t in tenants]
    tenant_ids = [t for t in tenant_ids if t]
    logger.info(f"Scanning {len(tenant_ids)} source tenants for API tokens...")

    role_map, _ = build_role_map(source_client, destination_client)

    output_path = _output_path()
    fresh = not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    created = skipped = failed = 0
    scanned_with_tokens = 0

    # 0600 so the secrets are not world-readable.
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        if fresh:
            writer.writeheader()
            handle.flush()

        for tenant_id in tenant_ids:
            try:
                source_tokens = _list_tokens(source_client, tenant_id)
            except Exception as e:
                log_error(f"  {tenant_id}: could not list source tokens: {e}")
                continue
            if not source_tokens:
                continue
            scanned_with_tokens += 1

            try:
                existing = {(t.get("description") or "").strip()
                            for t in _list_tokens(destination_client, tenant_id)}
            except Exception as e:
                log_error(f"  {tenant_id}: could not list destination tokens: {e}")
                continue

            for token in source_tokens:
                description = (token.get("description") or "").strip()
                expires_in = _minutes_until(token.get("expires"))
                if token.get("expires") and expires_in is None and not include_expired:
                    skipped += 1
                    continue
                if description and description in existing:
                    skipped += 1
                    continue

                source_role_ids = token.get("roleIds") or []
                mapped = [role_map[r] for r in source_role_ids if r in role_map]
                if source_role_ids and not mapped:
                    log_warning(f"  {tenant_id}: skipping {description!r} — none of its "
                                "roles exist in the destination")
                    skipped += 1
                    continue
                if not mapped:
                    log_warning(f"  {tenant_id}: skipping {description!r} — no roles to "
                                "assign (roleIds or permissionIds are required)")
                    skipped += 1
                    continue

                body = {
                    "description": description,
                    "metadata": token.get("metadata") or {},
                    "roleIds": sorted(set(mapped)),
                }
                if expires_in:
                    body["expiresInMinutes"] = expires_in

                try:
                    response = paced_request(
                        destination_client, "POST",
                        f"{destination_client.base_url}{CREATE_PATH}",
                        headers={"frontegg-tenant-id": tenant_id},
                        json_data=body, min_interval=INTERVAL_GENERAL,
                    )
                    payload = response.json()
                except Exception as e:
                    failed += 1
                    log_error(f"  {tenant_id}: failed to create {description!r}: {e}")
                    continue

                # Written immediately: the secret is shown exactly once.
                writer.writerow({
                    "tenantId": tenant_id,
                    "description": description,
                    "newClientId": payload.get("clientId", ""),
                    "newSecret": payload.get("secret", ""),
                    "roleIds": "|".join(body["roleIds"]),
                    "expiresInMinutes": body.get("expiresInMinutes", ""),
                    "sourceClientId": token.get("clientId", ""),
                    "createdAt": payload.get("createdAt", ""),
                })
                handle.flush()
                os.fsync(handle.fileno())
                created += 1
                log_success(f"  {tenant_id}: created {description!r} -> "
                            f"{payload.get('clientId')}")

    logger.info(f"Tenants with API tokens: {scanned_with_tokens}")
    log_success(f"Tokens created: {created}")
    if skipped:
        logger.info(f"Skipped (already present or expired): {skipped}")
    if failed:
        log_error(f"Failed: {failed}")
    if created:
        log_warning(f"NEW SECRETS written to {output_path} (mode 0600). Share them over a "
                    "secure channel and delete the local copy when done.")
