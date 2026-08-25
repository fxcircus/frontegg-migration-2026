"""Migrate authentication/security policies — environment level plus tenant overrides.

Closes the "forced-MFA setting was not carried over" gap: the security policies
configured under Authentication in the portal live under
/identity/resources/configurations and are not touched by any other step. Seven
environment-level policies are covered — MFA enforcement, MFA method config,
lockout, password complexity, password history, captcha, and session
management — plus per-tenant overrides of the three that support them
(MFA enforcement, lockout, sessions).

Idempotence: each run GETs both sides, compares the payloads with server
fields stripped, and writes only when they differ. Nothing is ever deleted or
removed from the destination; the step is additive and safely re-runnable.

Per-tenant overrides — the effective-vs-default heuristic:
  Frontegg has no "list tenant overrides" endpoint; a tenant-scoped GET (via
  the frontegg-tenant-id header) returns the EFFECTIVE policy — the tenant's
  own if one is set, otherwise the environment default. An override is
  therefore detected as "tenant-scoped value differs from the environment-level
  value" on the source. A tenant override that happens to exactly equal the
  environment default is indistinguishable from no override and is skipped —
  harmless, since the effective behavior is identical either way. The scan
  costs ~3 GETs per source tenant (one per overridable policy), which is
  acceptable at INTERVAL_GENERAL pacing.

Captcha credentials (siteKey/secretKey) are copied verbatim but never logged.
"""

import os

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, paced_request,
                                   strip_server_fields)

logger = get_logger()

CONFIG_BASE = "/identity/resources/configurations"

# Environment-level policies, in migration order.
#   write: "post-or-patch" — POST when the destination GET 404s, PATCH otherwise
#          (a second POST returns 409 "Security policy already exists").
#          "post"          — POST upserts in place, on create and update alike.
#   body:  "stripped"      — source payload minus server-generated fields.
#          "verbatim"      — source payload exactly as returned by GET.
#          "fields"        — only the keys listed under "fields".
#          "captcha"       — stripped, minus None values, gated on non-empty keys.
ENV_POLICIES = (
    {"name": "MFA policy", "path": CONFIG_BASE + "/v1/mfa-policy",
     "write": "post-or-patch", "body": "stripped", "tenant_overrides": True},
    {"name": "MFA method config", "path": CONFIG_BASE + "/v1/mfa",
     "write": "post", "body": "verbatim"},
    {"name": "Lockout policy", "path": CONFIG_BASE + "/v1/lockout-policy",
     "write": "post", "body": "stripped", "tenant_overrides": True},
    {"name": "Password complexity", "path": CONFIG_BASE + "/v1/password",
     "write": "post", "body": "stripped"},
    {"name": "Password history policy", "path": CONFIG_BASE + "/v1/password-history-policy",
     "write": "post-or-patch", "body": "fields", "fields": ("enabled", "historySize")},
    {"name": "Captcha policy", "path": CONFIG_BASE + "/v1/captcha-policy",
     "write": "post", "body": "captcha"},
    {"name": "Session configuration", "path": CONFIG_BASE + "/sessions/v1",
     "write": "post", "body": "verbatim", "tenant_overrides": True},
)

TENANT_OVERRIDE_POLICIES = tuple(s for s in ENV_POLICIES if s.get("tenant_overrides"))


def _read_policy(client, path, tenant_id=None):
    """GET one policy; None when absent (404) or unset (empty body).

    404 is how these endpoints report "not configured" (e.g. password-history
    returns 404 "Security policy not found"), and the sessions endpoint returns
    {} when unset — both normalize to None here.
    """
    headers = {"frontegg-tenant-id": tenant_id} if tenant_id else None
    response = paced_request(client, "GET", f"{client.base_url}{path}",
                             headers=headers, allow_status=(404,),
                             min_interval=INTERVAL_GENERAL)
    if response.status_code == 404:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload or None


def _comparable(policy):
    """Comparison form of a policy: server fields and tenantId stripped.

    tenantId is stripped too so a tenant-scoped effective payload can be
    compared against the environment-level one without a false mismatch.
    """
    return strip_server_fields(policy, extra=("tenantId",))


def _body_for(spec, policy):
    """The write body for one source policy, per the spec's body mode."""
    mode = spec["body"]
    if mode == "verbatim":
        return policy
    if mode == "fields":
        return {key: policy.get(key) for key in spec["fields"]}
    if mode == "captcha":
        stripped = _comparable(policy)
        return {k: v for k, v in stripped.items() if v is not None}
    return _comparable(policy)


def _write_policy(client, spec, body, tenant_id, dest_exists):
    """POST or PATCH one policy into the destination; returns the outcome label."""
    method = "PATCH" if spec["write"] == "post-or-patch" and dest_exists else "POST"
    headers = {"frontegg-tenant-id": tenant_id} if tenant_id else None
    paced_request(client, method, f"{client.base_url}{spec['path']}",
                  headers=headers, json_data=body, min_interval=INTERVAL_GENERAL)
    return "updated" if dest_exists else "created"


def _tenant_ids_of(tenants):
    ids = []
    for tenant in tenants or []:
        tenant_id = tenant.get("tenantId") or tenant.get("id")
        if tenant_id:
            ids.append(tenant_id)
    return ids


def _migrate_env_policies(source_client, destination_client):
    """Sync the seven environment-level policies; returns the source baselines."""
    counts = {"created": 0, "updated": 0, "in_sync": 0, "skipped": 0, "failed": 0}
    skipped = []
    source_env = {}

    for spec in ENV_POLICIES:
        name = spec["name"]
        # A non-404 read failure (e.g. 403 on a plan-gated route) must not
        # abort the whole migration run — count it and move on.
        try:
            source_policy = _read_policy(source_client, spec["path"])
        except Exception as e:
            counts["failed"] += 1
            source_env[spec["path"]] = None
            log_error(f"  {name}: source read failed — {e}")
            continue
        source_env[spec["path"]] = source_policy

        if source_policy is None:
            logger.info(f"  {name}: not configured in source — nothing to migrate.")
            counts["skipped"] += 1
            skipped.append(f"{name} (not configured in source)")
            continue
        if (spec["body"] == "captcha"
                and not (source_policy.get("siteKey") and source_policy.get("secretKey"))):
            logger.info(f"  {name}: siteKey/secretKey empty in source — skipping "
                        "(the API rejects captcha policies without keys).")
            counts["skipped"] += 1
            skipped.append(f"{name} (siteKey/secretKey empty in source)")
            continue

        try:
            dest_policy = _read_policy(destination_client, spec["path"])
        except Exception as e:
            counts["failed"] += 1
            log_error(f"  {name}: destination read failed — {e}")
            continue
        if _comparable(source_policy) == _comparable(dest_policy):
            logger.info(f"  {name}: already in sync.")
            counts["in_sync"] += 1
            continue

        try:
            outcome = _write_policy(destination_client, spec,
                                    _body_for(spec, source_policy),
                                    None, dest_policy is not None)
            counts[outcome] += 1
            log_success(f"  {name}: {outcome} in destination")
        except Exception as e:
            counts["failed"] += 1
            log_error(f"  {name}: write failed — {e}")

    log_success(f"Environment policies: {counts['created']} created, "
                f"{counts['updated']} updated, {counts['in_sync']} in sync, "
                f"{counts['skipped']} skipped")
    if counts["failed"]:
        log_error(f"Environment policies failed: {counts['failed']}")
    if skipped:
        log_warning(f"{len(skipped)} environment policies skipped:")
        for line in skipped[:10]:
            logger.info(f"    {line}")
    return source_env


def _migrate_tenant_overrides(source_client, destination_client, source_env):
    """Replay per-tenant overrides of MFA, lockout, and session policies.

    See the module docstring for the effective-vs-default heuristic this uses
    to detect overrides, and its ~3-GETs-per-tenant cost.
    """
    from migration_scripts.tenants import get_tenants

    logger.info("Scanning source tenants for per-tenant policy overrides "
                f"({len(TENANT_OVERRIDE_POLICIES)} GETs per tenant)...")
    source_tenant_ids = _tenant_ids_of(get_tenants(source_client))
    dest_tenant_ids = set(_tenant_ids_of(get_tenants(destination_client)))
    if not source_tenant_ids:
        log_warning("No tenants in the source account; no overrides to scan.")
        return

    counts = {"created": 0, "updated": 0, "in_sync": 0, "failed": 0}
    overrides_per_policy = {spec["name"]: 0 for spec in TENANT_OVERRIDE_POLICIES}
    missing_tenants = []

    for tenant_id in source_tenant_ids:
        for spec in TENANT_OVERRIDE_POLICIES:
            name = spec["name"]
            try:
                tenant_policy = _read_policy(source_client, spec["path"], tenant_id)
            except Exception as e:
                logger.debug(f"  {name} read failed for source tenant {tenant_id}: {e}")
                continue
            if tenant_policy is None:
                continue
            if _comparable(tenant_policy) == _comparable(source_env.get(spec["path"])):
                continue  # matches the environment default — no override to carry
            overrides_per_policy[name] += 1

            if tenant_id not in dest_tenant_ids:
                missing_tenants.append((tenant_id, name))
                continue
            try:
                dest_policy = _read_policy(destination_client, spec["path"], tenant_id)
                if _comparable(tenant_policy) == _comparable(dest_policy):
                    counts["in_sync"] += 1
                    continue
                outcome = _write_policy(destination_client, spec,
                                        _body_for(spec, tenant_policy),
                                        tenant_id, dest_policy is not None)
                counts[outcome] += 1
                log_success(f"  {tenant_id}: {name} override {outcome}")
            except Exception as e:
                counts["failed"] += 1
                log_error(f"  {tenant_id}: {name} override failed — {e}")

    total = sum(overrides_per_policy.values())
    if not total:
        log_success("No per-tenant policy overrides found in the source.")
        return
    logger.info("Override counts by policy: "
                + ", ".join(f"{name}: {n}" for name, n in overrides_per_policy.items()))
    log_success(f"Tenant overrides: {counts['created']} created, "
                f"{counts['updated']} updated, {counts['in_sync']} in sync, "
                f"{len(missing_tenants)} skipped (tenant missing)")
    if counts["failed"]:
        log_error(f"Tenant overrides failed: {counts['failed']}")
    if missing_tenants:
        log_warning(f"{len(missing_tenants)} overrides skipped because the tenant does "
                    "not exist in the destination (run MIGRATE_TENANTS first):")
        for tenant_id, name in missing_tenants[:10]:
            logger.info(f"    {tenant_id} | {name}")


def migrate_auth_policies(source_client, destination_client):
    log_section("Authentication & Security Policies")

    source_env = _migrate_env_policies(source_client, destination_client)

    overrides = os.getenv("AUTH_POLICIES_TENANT_OVERRIDES", "true")
    if overrides.strip().lower() not in ("1", "true", "yes", "on"):
        logger.info("Per-tenant policy overrides disabled "
                    "(AUTH_POLICIES_TENANT_OVERRIDES is off).")
        return
    _migrate_tenant_overrides(source_client, destination_client, source_env)
