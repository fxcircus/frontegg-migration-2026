"""Read-only source vs destination reconciliation report.

Standalone pre/post-cutover completeness check (run it like delete_account_data.py:
`python3 verify_migration.py`). It compares live state on BOTH accounts and writes
a JSON report plus a one-screen console summary, exiting 0 when everything matches
and 1 when it does not — so CI can gate a cutover on it.

Safe to run at ANY time: every request it makes is a GET, and BOTH clients are
constructed read_only=True (verification never writes anywhere, destination
included — the session layer enforces it too).

Sections:
  endpoint_parity        the "users/v2 404s on one region" probe — same GETs on
                         both stacks, any status-code mismatch is tabled
  tenants                missing/extra tenants and name drift
  users                  missing users (THE cutover delta list, `--since` aware),
                         plus per-attribute drift on common users
  memberships            missing (email, tenant) pairs and role drift by role KEY
  roles_and_permissions  roles by (scope, key); permissions by key; categories by name
  groups                 per common tenant, group names + member/role-count drift
  policies               env-level security policies, server fields stripped
  applications           application names
  api_tokens (--tokens)  per-tenant token counts + description sets (never secrets)
  sso (--sso)            per-tenant SSO config counts + enabled flags

Recreated secrets (API tokens, captcha secretKey, ...) are never written into the
report — sensitive values are replaced with "<set>"/"<empty>" before serializing.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from utility.frontegg_client import FronteggClient
from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (GLOBAL_SCOPE, INTERVAL_GENERAL, get_all_roles,
                                   get_all_users, index_users_by_email,
                                   membership_role_objs, memberships_of,
                                   paced_request, strip_server_fields)

logger = get_logger()

EXAMPLES = 10  # named examples printed to the console per finding

# Endpoints probed identically on both stacks for status-code parity.
PARITY_PATHS = (
    "/identity/resources/users/v2?_limit=1",
    "/identity/resources/users/v2?_limit=1&_usersRelations=groups",
    "/identity/resources/users/v3?_limit=1",
    "/identity/resources/roles/v2?_limit=1",
    "/identity/resources/permissions/v1",
    "/identity/resources/permissions/v1/categories",
    "/identity/resources/configurations/v1",
    "/identity/resources/configurations/v1/mfa-policy",
    "/identity/resources/configurations/v1/mfa",
    "/identity/resources/configurations/v1/lockout-policy",
    "/identity/resources/configurations/v1/password",
    "/identity/resources/configurations/v1/captcha-policy",
    "/identity/resources/configurations/sessions/v1",
    "/identity/resources/groups/v1/config",
    "/tenants/resources/tenants/v2?_limit=1",
)

# Environment-level policies compared field-by-field (404 = unset).
POLICY_ENDPOINTS = (
    ("mfa-policy", "/identity/resources/configurations/v1/mfa-policy"),
    ("mfa", "/identity/resources/configurations/v1/mfa"),
    ("lockout-policy", "/identity/resources/configurations/v1/lockout-policy"),
    ("password", "/identity/resources/configurations/v1/password"),
    ("password-history-policy",
     "/identity/resources/configurations/v1/password-history-policy"),
    ("captcha-policy", "/identity/resources/configurations/v1/captcha-policy"),
    ("sessions", "/identity/resources/configurations/sessions/v1"),
)

TENANTS_PATH = "/tenants/resources/tenants/v2"
GROUPS_PATH = "/identity/resources/groups/v1"          # migration_scripts/groups.py
APPLICATIONS_PATH = "/applications/resources/applications/v1?_excludeAgents=true"
                                                       # migration_scripts/applications.py
TENANT_TOKENS_PATH = "/identity/resources/tenants/api-tokens/v1"
                                                # migration_scripts/tenant_api_tokens.py
SSO_PATH = "/team/resources/sso/v1/configurations"     # migration_scripts/sso_configurations.py

SECTION_ORDER = ("endpoint_parity", "tenants", "users", "memberships",
                 "roles_and_permissions", "groups", "policies", "applications",
                 "api_tokens", "sso")

# 400/403/404 are answers, not failures, when probing optional per-tenant resources.
PROBE_STATUSES = (400, 403, 404)
# For the parity section every status code IS the datum, so nothing may raise.
ANY_STATUS = tuple(range(300, 600))

_SENSITIVE_KEY_HINTS = ("secret", "token", "apikey", "api_key")


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _is_sensitive_key(key):
    lowered = str(key).lower()
    return lowered == "password" or any(h in lowered for h in _SENSITIVE_KEY_HINTS)


def _redact(value):
    """Recursive copy with sensitive SCALAR values replaced by "<set>"/"<empty>".

    Applied to the WHOLE report right before it is serialized, so no secretKey,
    token, or password value can ever land on disk regardless of which section
    produced it. Containers are always recursed into — even under a sensitive
    name like "api_tokens" — because structure is not a secret; only scalar
    values under sensitive keys are. Comparisons always run on the raw values.
    """
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                out[key] = _redact(val)
            elif _is_sensitive_key(key):
                out[key] = "<set>" if val else "<empty>"
            else:
                out[key] = val
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _parse_iso(value):
    """ISO8601 -> aware UTC datetime, or None. Naive input is assumed UTC."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _get_json(client, path, tenant_id=None, allow_status=PROBE_STATUSES,
              min_interval=INTERVAL_GENERAL):
    """(status_code, parsed body or None) for one GET. Never writes anything."""
    headers = {"frontegg-tenant-id": tenant_id} if tenant_id else None
    response = paced_request(client, "GET", f"{client.base_url}{path}",
                             headers=headers, min_interval=min_interval,
                             allow_status=allow_status)
    if response.status_code != 200:
        return response.status_code, None
    try:
        return 200, response.json()
    except ValueError:
        return 200, None


def _get_tenants(client, page_size=200):
    """Every tenant in the environment (same pagination as get_all_roles)."""
    url = f"{client.base_url}{TENANTS_PATH}"
    tenants = []
    params = {"_limit": page_size, "_offset": 0}
    while True:
        response = paced_request(client, "GET", url, params=params)
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        tenants.extend(items)
        meta = (payload.get("_metadata") or {}) if isinstance(payload, dict) else {}
        total_pages = meta.get("totalPages")
        if not items or total_pages is None or params["_offset"] + 1 >= total_pages:
            break
        params = {"_limit": page_size, "_offset": params["_offset"] + 1}
    return tenants


def _tenant_id_of(tenant):
    if not isinstance(tenant, dict):
        return None
    return tenant.get("tenantId") or tenant.get("id")


def _deep_strip(value):
    """strip_server_fields, but through nested dicts/lists (for policy bodies)."""
    if isinstance(value, dict):
        return {k: _deep_strip(v) for k, v in strip_server_fields(value).items()}
    if isinstance(value, list):
        return [_deep_strip(item) for item in value]
    return value


def _print_examples(entries, render):
    for entry in entries[:EXAMPLES]:
        logger.info(f"    {render(entry)}")
    if len(entries) > EXAMPLES:
        logger.info(f"    ... and {len(entries) - EXAMPLES} more (see report)")


# ---------------------------------------------------------------------------
# Section 1: endpoint parity
# ---------------------------------------------------------------------------

def check_endpoint_parity(source_client, destination_client):
    """Same GET on both stacks; a status-code mismatch means the regions differ."""
    statuses, mismatches, unavailable = [], [], []
    for path in PARITY_PATHS:
        src_status, _ = _get_json(source_client, path, allow_status=ANY_STATUS)
        dst_status, _ = _get_json(destination_client, path, allow_status=ANY_STATUS)
        entry = {"path": path, "source": src_status, "destination": dst_status}
        statuses.append(entry)
        if src_status != dst_status:
            mismatches.append(entry)
        elif src_status != 200:
            # Non-200 on BOTH sides: the stacks agree, so it is a note, not a diff.
            unavailable.append(entry)

    if mismatches:
        log_warning(f"{len(mismatches)} endpoint(s) answer differently per region:")
        _print_examples(mismatches, lambda e:
                        f"{e['path']} — source {e['source']} / dest {e['destination']}")
    else:
        log_success(f"All {len(PARITY_PATHS)} probed endpoints answer identically.")
    if unavailable:
        log_warning(f"{len(unavailable)} endpoint(s) are consistently unavailable "
                    "on BOTH stacks (agreement, not a mismatch):")
        _print_examples(unavailable, lambda e: f"{e['path']} [{e['source']}]")

    return len(mismatches), {"mismatches": mismatches,
                             "consistentlyUnavailable": unavailable,
                             "statuses": statuses}


# ---------------------------------------------------------------------------
# Section 2: tenants
# ---------------------------------------------------------------------------

def check_tenants(source_tenants, dest_tenants):
    source_by_id = {_tenant_id_of(t): t for t in source_tenants if _tenant_id_of(t)}
    dest_by_id = {_tenant_id_of(t): t for t in dest_tenants if _tenant_id_of(t)}

    missing = [{"tenantId": tid, "name": source_by_id[tid].get("name")}
               for tid in sorted(set(source_by_id) - set(dest_by_id))]
    extra_count = len(set(dest_by_id) - set(source_by_id))
    name_drift = []
    for tid in sorted(set(source_by_id) & set(dest_by_id)):
        src_name = (source_by_id[tid].get("name") or "").strip()
        dst_name = (dest_by_id[tid].get("name") or "").strip()
        if src_name != dst_name:
            name_drift.append({"tenantId": tid, "source": src_name,
                               "destination": dst_name})

    logger.info(f"  {len(source_by_id)} source / {len(dest_by_id)} destination tenants")
    if missing:
        log_warning(f"{len(missing)} source tenants missing in the destination:")
        _print_examples(missing, lambda e: f"{e['tenantId']} ({e['name']!r})")
    if name_drift:
        log_warning(f"{len(name_drift)} common tenants have a different name:")
        _print_examples(name_drift, lambda e:
                        f"{e['tenantId']}: {e['source']!r} -> {e['destination']!r}")
    if extra_count:
        logger.info(f"  {extra_count} tenants exist only in the destination "
                    "(extras are noted, not counted as drift)")
    if not missing and not name_drift:
        log_success("Tenants are in sync.")

    return len(missing) + len(name_drift), {
        "sourceTotal": len(source_by_id), "destinationTotal": len(dest_by_id),
        "missingInDestination": missing, "extraInDestinationCount": extra_count,
        "nameDrift": name_drift,
    }


# ---------------------------------------------------------------------------
# Section 3: users
# ---------------------------------------------------------------------------

def _norm_metadata(value):
    """Parse-compare metadata: JSON strings become objects when possible.

    Every spelling of "nothing there" — None, "", "{}", and the JSON string
    "null" — normalizes to {} so none of them ever counts as drift.
    """
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return {} if parsed is None else parsed
    return value


_USER_ATTRIBUTES = (
    ("isLocked", lambda u: bool(u.get("isLocked"))),
    ("verified", lambda u: bool(u.get("verified"))),
    ("name", lambda u: (u.get("name") or "").strip()),
    ("phoneNumber", lambda u: (u.get("phoneNumber") or "").strip()),
    ("metadata", lambda u: _norm_metadata(u.get("metadata"))),
)


def check_users(source_users, dest_users, since):
    source_by_email = index_users_by_email(source_users)
    dest_by_email = index_users_by_email(dest_users)

    missing, suppressed_by_since = [], 0
    for email in set(source_by_email) - set(dest_by_email):
        created_at = source_by_email[email].get("createdAt")
        if since is not None:
            created = _parse_iso(created_at)
            # Unparseable timestamps stay flagged — safer than hiding a gap.
            if created is not None and created <= since:
                suppressed_by_since += 1
                continue
        missing.append({"email": email, "createdAt": created_at})
    # Newest first: this is the cutover delta list, so recency is what matters.
    missing.sort(key=lambda e: (_parse_iso(e["createdAt"]) or
                                datetime.min.replace(tzinfo=timezone.utc)),
                 reverse=True)
    extra_count = len(set(dest_by_email) - set(source_by_email))

    drift = {name: [] for name, _ in _USER_ATTRIBUTES}
    for email in sorted(set(source_by_email) & set(dest_by_email)):
        src_user, dst_user = source_by_email[email], dest_by_email[email]
        for name, norm in _USER_ATTRIBUTES:
            src_val, dst_val = norm(src_user), norm(dst_user)
            if src_val != dst_val:
                drift[name].append({"email": email, "source": src_val,
                                    "destination": dst_val})
    drift_total = sum(len(entries) for entries in drift.values())

    logger.info(f"  {len(source_by_email)} source / {len(dest_by_email)} "
                "destination users (unique emails)")
    if missing:
        log_warning(f"{len(missing)} source users missing in the destination "
                    "(the cutover delta list, newest first):")
        _print_examples(missing, lambda e: f"{e['email']} (created {e['createdAt']})")
    if suppressed_by_since:
        logger.info(f"  {suppressed_by_since} missing users created before --since "
                    "were not flagged")
    if extra_count:
        logger.info(f"  {extra_count} users exist only in the destination "
                    "(extras are noted, not counted as drift)")
    for name, entries in drift.items():
        if entries:
            log_warning(f"{len(entries)} common users differ on {name!r}:")
            _print_examples(entries, lambda e:
                            f"{e['email']}: {e['source']!r} -> {e['destination']!r}")
    if not missing and not drift_total:
        log_success("Users are in sync.")

    return len(missing) + drift_total, {
        "sourceTotal": len(source_by_email), "destinationTotal": len(dest_by_email),
        "missingInDestination": missing,
        "missingSuppressedBySince": suppressed_by_since,
        "extraInDestinationCount": extra_count,
        "attributeDrift": drift,
        "attributeDriftCounts": {n: len(e) for n, e in drift.items()},
    }


# ---------------------------------------------------------------------------
# Section 4: memberships
# ---------------------------------------------------------------------------

def check_memberships(source_users, dest_users):
    """Tenant memberships and per-tenant roles, compared by role KEY.

    Role ids never carry across accounts, so drift is judged the way
    user_tenant_roles.py maps: by the role keys attached to each membership.
    A key present in the source but absent in the destination is drift; extra
    destination keys are ignored (the migration is additive).
    """
    dest_by_email = index_users_by_email(dest_users)

    missing, role_drift = [], []
    common_users = common_memberships = 0
    for user in source_users:
        email = (user.get("email") or "").strip().lower()
        dest_user = dest_by_email.get(email) if email else None
        if not dest_user:
            continue  # absent users are the users section's finding, not ours
        common_users += 1

        source_roles = membership_role_objs(user)
        dest_roles = membership_role_objs(dest_user)
        dest_tenants = set(memberships_of(dest_user))

        for tenant_id, roles in sorted(source_roles.items()):
            if tenant_id not in dest_tenants:
                missing.append({"email": email, "tenantId": tenant_id})
                continue
            common_memberships += 1
            source_keys = {r.get("key") for r in roles if r.get("key")}
            dest_keys = {r.get("key") for r in dest_roles.get(tenant_id, [])
                         if r.get("key")}
            only_in_source = sorted(source_keys - dest_keys)
            if only_in_source:
                role_drift.append({"email": email, "tenantId": tenant_id,
                                   "missingRoleKeys": only_in_source})

    logger.info(f"  {common_users} common users / {common_memberships} common memberships")
    if missing:
        log_warning(f"{len(missing)} source memberships missing in the destination:")
        _print_examples(missing, lambda e: f"{e['tenantId']} | {e['email']}")
    if role_drift:
        log_warning(f"{len(role_drift)} common memberships are missing source role keys:")
        _print_examples(role_drift, lambda e:
                        f"{e['tenantId']} | {e['email']} — {e['missingRoleKeys']}")
    if not missing and not role_drift:
        log_success("Memberships and membership roles are in sync.")

    return len(missing) + len(role_drift), {
        "commonUsers": common_users, "commonMemberships": common_memberships,
        "missingInDestination": missing, "roleDrift": role_drift,
    }


# ---------------------------------------------------------------------------
# Section 5: roles and permissions
# ---------------------------------------------------------------------------

def check_roles_and_permissions(source_client, destination_client):
    def scope_key(role):
        return (role.get("tenantId") or GLOBAL_SCOPE, role.get("key"))

    source_roles = {scope_key(r) for r in get_all_roles(source_client)}
    dest_roles = {scope_key(r) for r in get_all_roles(destination_client)}
    missing_roles = [{"scope": s if s != GLOBAL_SCOPE else "account-level", "key": k}
                     for s, k in sorted(source_roles - dest_roles,
                                        key=lambda p: (str(p[0]), str(p[1])))]

    _, src_perms = _get_json(source_client, "/identity/resources/permissions/v1")
    _, dst_perms = _get_json(destination_client, "/identity/resources/permissions/v1")
    src_perm_keys = {p.get("key") for p in (src_perms or []) if p.get("key")}
    dst_perm_keys = {p.get("key") for p in (dst_perms or []) if p.get("key")}
    missing_perms = sorted(src_perm_keys - dst_perm_keys)

    _, src_cats = _get_json(source_client,
                            "/identity/resources/permissions/v1/categories")
    _, dst_cats = _get_json(destination_client,
                            "/identity/resources/permissions/v1/categories")
    src_cat_names = {c.get("name") for c in (src_cats or []) if c.get("name")}
    dst_cat_names = {c.get("name") for c in (dst_cats or []) if c.get("name")}
    missing_cats = sorted(src_cat_names - dst_cat_names)

    logger.info(f"  Roles: {len(source_roles)} source / {len(dest_roles)} destination; "
                f"permissions: {len(src_perm_keys)} / {len(dst_perm_keys)}; "
                f"categories: {len(src_cat_names)} / {len(dst_cat_names)}")
    if missing_roles:
        log_warning(f"{len(missing_roles)} source roles missing in the destination:")
        _print_examples(missing_roles, lambda e: f"key={e['key']!r} scope={e['scope']}")
    if missing_perms:
        log_warning(f"{len(missing_perms)} source permissions missing in the destination:")
        _print_examples(missing_perms, lambda k: k)
    if missing_cats:
        log_warning(f"{len(missing_cats)} source permission categories missing:")
        _print_examples(missing_cats, lambda n: n)
    if not (missing_roles or missing_perms or missing_cats):
        log_success("Roles, permissions, and categories are in sync.")

    diff = len(missing_roles) + len(missing_perms) + len(missing_cats)
    return diff, {
        "missingRoles": missing_roles,
        "extraRolesInDestinationCount": len(dest_roles - source_roles),
        "missingPermissionKeys": missing_perms,
        "extraPermissionsInDestinationCount": len(dst_perm_keys - src_perm_keys),
        "missingCategoryNames": missing_cats,
        "extraCategoriesInDestinationCount": len(dst_cat_names - src_cat_names),
    }


# ---------------------------------------------------------------------------
# Section 6: groups
# ---------------------------------------------------------------------------

def _extract_groups(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("groups"), list):
        return payload["groups"]
    return []


def check_groups(source_client, destination_client, common_tenant_ids):
    """Per common tenant: group-name diff plus member/role-count drift by name."""
    logger.info(f"  Comparing groups across {len(common_tenant_ids)} common tenants "
                "(two GETs per tenant)...")
    missing, count_drift, unavailable = [], [], []
    extra_count = 0
    path = f"{GROUPS_PATH}?_groupsRelations=rolesAndUsers"

    for tenant_id in common_tenant_ids:
        src_status, src_payload = _get_json(source_client, path, tenant_id=tenant_id)
        dst_status, dst_payload = _get_json(destination_client, path,
                                            tenant_id=tenant_id)
        if src_status != 200 or dst_status != 200:
            unavailable.append({"tenantId": tenant_id, "source": src_status,
                                "destination": dst_status})
            continue

        src_groups = {g.get("name"): g for g in _extract_groups(src_payload)
                      if g.get("name")}
        dst_groups = {g.get("name"): g for g in _extract_groups(dst_payload)
                      if g.get("name")}
        for name in sorted(set(src_groups) - set(dst_groups)):
            missing.append({"tenantId": tenant_id, "group": name})
        extra_count += len(set(dst_groups) - set(src_groups))
        for name in sorted(set(src_groups) & set(dst_groups)):
            src_g, dst_g = src_groups[name], dst_groups[name]
            entry = {"tenantId": tenant_id, "group": name,
                     "sourceMembers": len(src_g.get("users") or []),
                     "destinationMembers": len(dst_g.get("users") or []),
                     "sourceRoles": len(src_g.get("roles") or []),
                     "destinationRoles": len(dst_g.get("roles") or [])}
            if (entry["sourceMembers"] != entry["destinationMembers"]
                    or entry["sourceRoles"] != entry["destinationRoles"]):
                count_drift.append(entry)

    if missing:
        log_warning(f"{len(missing)} source groups missing in the destination:")
        _print_examples(missing, lambda e: f"{e['tenantId']} | {e['group']!r}")
    if count_drift:
        log_warning(f"{len(count_drift)} same-name groups drift on member/role counts:")
        _print_examples(count_drift, lambda e:
                        f"{e['tenantId']} | {e['group']!r} — members "
                        f"{e['sourceMembers']}->{e['destinationMembers']}, roles "
                        f"{e['sourceRoles']}->{e['destinationRoles']}")
    if unavailable:
        log_warning(f"{len(unavailable)} tenants could not be compared "
                    "(groups endpoint non-200 on at least one side):")
        _print_examples(unavailable, lambda e:
                        f"{e['tenantId']} [source {e['source']} / "
                        f"dest {e['destination']}]")
    if not missing and not count_drift:
        log_success("Groups are in sync on every comparable tenant.")

    return len(missing) + len(count_drift), {
        "missingInDestination": missing, "countDrift": count_drift,
        "extraInDestinationCount": extra_count, "unavailableTenants": unavailable,
    }


# ---------------------------------------------------------------------------
# Section 7: policies
# ---------------------------------------------------------------------------

def check_policies(source_client, destination_client):
    """Environment security policies, compared after stripping server fields.

    404 means the policy was never set; unset on both sides is agreement. 400/403
    means the route cannot be read here — noted, not counted, because a value we
    cannot see is not evidence of drift.
    """
    details, differs, unreadable = {}, [], []
    for name, path in POLICY_ENDPOINTS:
        src_status, src_body = _get_json(source_client, path)
        dst_status, dst_body = _get_json(destination_client, path)

        if src_status in (400, 403) or dst_status in (400, 403):
            unreadable.append(name)
            details[name] = {"status": "unreadable", "source": src_status,
                             "destination": dst_status}
            continue

        src_norm = _deep_strip(src_body) if src_status == 200 else None
        dst_norm = _deep_strip(dst_body) if dst_status == 200 else None
        if src_norm == dst_norm:
            state = "unset-on-both" if src_norm is None else "equal"
            details[name] = {"status": state}
        else:
            differs.append(name)
            # Raw values were compared above; only redacted copies are stored
            # (the report-wide redaction pass also covers e.g. captcha secretKey).
            details[name] = {"status": "differs", "source": src_norm,
                             "destination": dst_norm}

    if differs:
        log_warning(f"{len(differs)} policies differ between the accounts:")
        _print_examples(differs, lambda n: n)
    if unreadable:
        log_warning(f"{len(unreadable)} policies could not be read on at least "
                    f"one side: {unreadable}")
    if not differs:
        log_success("All readable policies match.")

    return len(differs), details


# ---------------------------------------------------------------------------
# Section 8: applications
# ---------------------------------------------------------------------------

def check_applications(source_client, destination_client):
    src_status, src_apps = _get_json(source_client, APPLICATIONS_PATH)
    dst_status, dst_apps = _get_json(destination_client, APPLICATIONS_PATH)
    if src_status != 200 or dst_status != 200:
        raise RuntimeError(f"applications endpoint non-200 "
                           f"(source {src_status} / dest {dst_status})")

    src_names = {a.get("name") for a in (src_apps or []) if a.get("name")}
    dst_names = {a.get("name") for a in (dst_apps or []) if a.get("name")}
    missing = sorted(src_names - dst_names)
    extra_count = len(dst_names - src_names)

    logger.info(f"  {len(src_names)} source / {len(dst_names)} destination applications")
    if missing:
        log_warning(f"{len(missing)} source applications missing in the destination:")
        _print_examples(missing, lambda n: n)
    else:
        log_success("Applications are in sync.")

    return len(missing), {"sourceTotal": len(src_names),
                          "destinationTotal": len(dst_names),
                          "missingInDestination": missing,
                          "extraInDestinationCount": extra_count}


# ---------------------------------------------------------------------------
# Section 9: api tokens (opt-in)
# ---------------------------------------------------------------------------

def check_api_tokens(source_client, destination_client, common_tenant_ids):
    """Per-tenant token count + description parity.

    Recreated tokens have new ids and secrets by design, so count parity and the
    description set are what matters here. Secrets are never read into the detail.
    """
    logger.info(f"  Comparing API tokens across {len(common_tenant_ids)} common "
                "tenants (two GETs per tenant)...")
    drift, unavailable = [], []

    for tenant_id in common_tenant_ids:
        src_status, src_payload = _get_json(source_client, TENANT_TOKENS_PATH,
                                            tenant_id=tenant_id)
        dst_status, dst_payload = _get_json(destination_client, TENANT_TOKENS_PATH,
                                            tenant_id=tenant_id)
        if src_status != 200 or dst_status != 200:
            unavailable.append({"tenantId": tenant_id, "source": src_status,
                                "destination": dst_status})
            continue

        # The route returns a plain array, not a pagination wrapper.
        src_tokens = src_payload if isinstance(src_payload, list) else \
            (src_payload or {}).get("items", [])
        dst_tokens = dst_payload if isinstance(dst_payload, list) else \
            (dst_payload or {}).get("items", [])
        src_desc = sorted((t.get("description") or "") for t in src_tokens)
        dst_desc = sorted((t.get("description") or "") for t in dst_tokens)
        if len(src_tokens) != len(dst_tokens) or src_desc != dst_desc:
            drift.append({"tenantId": tenant_id,
                          "sourceCount": len(src_tokens),
                          "destinationCount": len(dst_tokens),
                          "missingDescriptions":
                              sorted(set(src_desc) - set(dst_desc))})

    if drift:
        log_warning(f"{len(drift)} tenants drift on API tokens:")
        _print_examples(drift, lambda e:
                        f"{e['tenantId']}: {e['sourceCount']} -> "
                        f"{e['destinationCount']} tokens, missing descriptions "
                        f"{e['missingDescriptions']}")
    else:
        log_success("API token counts and descriptions match on every "
                    "comparable tenant.")
    if unavailable:
        log_warning(f"{len(unavailable)} tenants could not be compared "
                    "(token endpoint non-200 on at least one side).")

    return len(drift), {"drift": drift, "unavailableTenants": unavailable,
                        "note": "recreated tokens have new ids — count parity "
                                "is what matters here"}


# ---------------------------------------------------------------------------
# Section 10: sso (opt-in)
# ---------------------------------------------------------------------------

def _extract_sso(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "configurations"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def check_sso(source_client, destination_client, common_tenant_ids):
    """Per-tenant SSO config count + enabled-flag parity."""
    logger.info(f"  Comparing SSO configurations across {len(common_tenant_ids)} "
                "common tenants (two GETs per tenant)...")
    drift, unavailable = [], []
    source_configured = dest_configured = 0

    for tenant_id in common_tenant_ids:
        src_status, src_payload = _get_json(source_client, SSO_PATH,
                                            tenant_id=tenant_id)
        dst_status, dst_payload = _get_json(destination_client, SSO_PATH,
                                            tenant_id=tenant_id)
        if src_status != 200 or dst_status != 200:
            unavailable.append({"tenantId": tenant_id, "source": src_status,
                                "destination": dst_status})
            continue

        src_configs = _extract_sso(src_payload)
        dst_configs = _extract_sso(dst_payload)
        if src_configs:
            source_configured += 1
        if dst_configs:
            dest_configured += 1
        src_enabled = sorted(bool(c.get("enabled")) for c in src_configs)
        dst_enabled = sorted(bool(c.get("enabled")) for c in dst_configs)
        if len(src_configs) != len(dst_configs) or src_enabled != dst_enabled:
            drift.append({"tenantId": tenant_id,
                          "sourceCount": len(src_configs),
                          "destinationCount": len(dst_configs),
                          "sourceEnabled": src_enabled,
                          "destinationEnabled": dst_enabled})

    logger.info(f"  {source_configured} source / {dest_configured} destination "
                "tenants have SSO configured")
    if drift:
        log_warning(f"{len(drift)} tenants drift on SSO configurations:")
        _print_examples(drift, lambda e:
                        f"{e['tenantId']}: {e['sourceCount']} -> "
                        f"{e['destinationCount']} configs, enabled "
                        f"{e['sourceEnabled']} -> {e['destinationEnabled']}")
    else:
        log_success("SSO configurations match on every comparable tenant.")
    if unavailable:
        log_warning(f"{len(unavailable)} tenants could not be compared "
                    "(SSO endpoint non-200 on at least one side).")

    return len(drift), {"sourceTenantsConfigured": source_configured,
                        "destinationTenantsConfigured": dest_configured,
                        "drift": drift, "unavailableTenants": unavailable}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def main(argv=None):
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Read-only source vs destination reconciliation report. "
                    "Makes ONLY GET requests to both accounts; safe to run at "
                    "any time. Exits 0 when no differences were found, 1 otherwise.")
    parser.add_argument(
        "--report", default=os.getenv("VERIFY_REPORT_PATH", "verify_report.json"),
        help="where to write the JSON report "
             "(default: verify_report.json, or env VERIFY_REPORT_PATH)")
    parser.add_argument(
        "--since", default=None, metavar="ISO8601",
        help="only flag missing users created after this instant "
             "(e.g. 2026-08-20T00:00:00Z) — the cutover delta")
    parser.add_argument(
        "--tokens", action="store_true",
        help="compare per-tenant API token counts/descriptions. COST: one extra "
             "GET per common tenant on EACH account, paced ~0.6s apiece")
    parser.add_argument(
        "--sso", action="store_true",
        help="compare per-tenant SSO configurations. COST: one extra GET per "
             "common tenant on EACH account, paced ~0.6s apiece")
    parser.add_argument(
        "--skip-groups", action="store_true",
        help="skip the per-tenant groups comparison (two GETs per common tenant)")
    args = parser.parse_args(argv)

    since = None
    if args.since:
        since = _parse_iso(args.since)
        if since is None:
            parser.error(f"--since is not a valid ISO8601 timestamp: {args.since!r}")

    log_section("Migration Verification (read-only)")
    logger.info("Both clients are read_only=True — this script never writes anywhere.")
    source_client = FronteggClient(os.getenv("BASE_URL_1"), os.getenv("CLIENT_ID_1"),
                                   os.getenv("API_KEY_1"), read_only=True)
    destination_client = FronteggClient(os.getenv("BASE_URL_2"),
                                        os.getenv("CLIENT_ID_2"),
                                        os.getenv("API_KEY_2"), read_only=True)

    sections, diffs = {}, {}

    def run(name, fn, *fn_args):
        """One section: any exception becomes an error entry, never a crash."""
        log_section(f"Verify: {name}")
        try:
            count, detail = fn(*fn_args)
        except Exception as e:
            log_error(f"  Section {name} failed: {e}")
            diffs[name] = None
            sections[name] = {"error": str(e)}
            return
        diffs[name] = count
        sections[name] = detail

    def fail(name, message):
        log_section(f"Verify: {name}")
        log_error(f"  {message}")
        diffs[name] = None
        sections[name] = {"error": message}

    def skip(name, reason):
        diffs[name] = "skipped"
        sections[name] = {"skipped": reason}

    run("endpoint_parity", check_endpoint_parity, source_client, destination_client)

    # Shared datasets — fetched once, reused by every dependent section.
    tenants_error = users_error = None
    source_tenants = dest_tenants = source_users = dest_users = None
    common_tenant_ids = []
    try:
        logger.info("Fetching tenants from both accounts...")
        source_tenants = _get_tenants(source_client)
        dest_tenants = _get_tenants(destination_client)
        src_ids = {_tenant_id_of(t) for t in source_tenants if _tenant_id_of(t)}
        dst_ids = {_tenant_id_of(t) for t in dest_tenants if _tenant_id_of(t)}
        common_tenant_ids = sorted(src_ids & dst_ids)
    except Exception as e:
        tenants_error = f"could not list tenants: {e}"
    try:
        logger.info("Fetching users from both accounts...")
        source_users = get_all_users(source_client)
        dest_users = get_all_users(destination_client)
    except Exception as e:
        users_error = f"could not list users: {e}"

    if tenants_error:
        fail("tenants", tenants_error)
    else:
        run("tenants", check_tenants, source_tenants, dest_tenants)

    if users_error:
        fail("users", users_error)
        fail("memberships", users_error)
    else:
        run("users", check_users, source_users, dest_users, since)
        run("memberships", check_memberships, source_users, dest_users)

    run("roles_and_permissions", check_roles_and_permissions,
        source_client, destination_client)

    if args.skip_groups:
        skip("groups", "--skip-groups")
    elif tenants_error:
        fail("groups", tenants_error)
    else:
        run("groups", check_groups, source_client, destination_client,
            common_tenant_ids)

    run("policies", check_policies, source_client, destination_client)
    run("applications", check_applications, source_client, destination_client)

    if not args.tokens:
        skip("api_tokens", "not requested (pass --tokens)")
    elif tenants_error:
        fail("api_tokens", tenants_error)
    else:
        run("api_tokens", check_api_tokens, source_client, destination_client,
            common_tenant_ids)

    if not args.sso:
        skip("sso", "not requested (pass --sso)")
    elif tenants_error:
        fail("sso", tenants_error)
    else:
        run("sso", check_sso, source_client, destination_client, common_tenant_ids)

    # Report — redacted as a whole so no secret value can reach disk.
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": source_client.base_url,
        "destination": destination_client.base_url,
        "sections": _redact(sections),
    }
    with open(args.report, "w") as handle:
        json.dump(report, handle, indent=2, default=str)

    # One-screen summary.
    summary = {}
    for name in SECTION_ORDER:
        value = diffs.get(name)
        if value is None:
            summary[name] = "ERROR"
        elif value == "skipped":
            summary[name] = f"skipped ({sections[name]['skipped']})"
        else:
            summary[name] = f"{value} diff(s)" if value else "in sync"
    logger.print_stats("Verification Summary (section -> differences)", summary)
    logger.info(f"Full report written to {os.path.abspath(args.report)}")

    total_diffs = sum(v for v in diffs.values() if isinstance(v, int))
    errors = sum(1 for v in diffs.values() if v is None)
    if errors:
        log_error(f"{errors} section(s) could not be verified — treating as failure.")
    if total_diffs:
        log_warning(f"{total_diffs} difference(s) found across all sections.")
        return 1
    if errors:
        return 1
    log_success("No differences found — destination matches source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
