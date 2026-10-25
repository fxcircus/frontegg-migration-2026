"""Shared API helpers for the user/token/hierarchy migration steps.

Adds three things the older per-script helpers don't provide:

1. Per-endpoint pacing plus exponential backoff on 429. Frontegg publishes very
   different limits per route (bulk invite is 5/min per environment on Launch,
   while most management routes are 100/min per IP), so each caller declares the
   interval it needs instead of sharing one global default.
2. Correct pagination for /identity/resources/users/v3. Its `_offset` is a PAGE
   NUMBER, not a record offset, so we follow the server-provided `_links.next`
   rather than computing offsets ourselves.
3. A complete source -> destination role map, built from both accounts' full role
   lists. Role IDs are environment-scoped and never portable across accounts.
"""

import re
import time
from urllib.parse import urlparse

import requests

from utility.logger import get_logger, log_error, log_warning

logger = get_logger()

# Path segments that look like generated ids (uuids and the like). Pacing keys
# collapse them so per-user/per-token URLs share one bucket per route instead of
# each unique URL bypassing the pacing entirely.
_ID_SEGMENT = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{15,}$")

# Documented Frontegg limits, expressed as the minimum seconds between calls.
# Launch-tier values so the defaults are safe on every plan; override per call.
INTERVAL_BULK_INVITE = 12.0   # 5/min per environment  -> 1 per 12s
INTERVAL_TENANTS = 2.0        # 30/min per environment -> 1 per 2s
INTERVAL_USER_LISTING = 2.0   # users/v2 per-tenant listings 429 at ~40/min
INTERVAL_GENERAL = 0.6        # 100/min per IP         -> 1 per 0.6s

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = [30, 60, 120, 240]

GLOBAL_SCOPE = "__GLOBAL__"

_last_call = {}


def _endpoint_key(url):
    parsed = urlparse(url)
    path = "/".join("{id}" if _ID_SEGMENT.match(segment) else segment
                    for segment in parsed.path.split("/"))
    return f"{parsed.netloc}{path}"


def paced_request(client, method, url, headers=None, json_data=None, params=None,
                  min_interval=INTERVAL_GENERAL, max_attempts=MAX_ATTEMPTS,
                  allow_status=()):
    """Make a request, pacing by endpoint and backing off through 429s.

    `allow_status` lists non-2xx codes to return instead of raising (e.g. 404
    when probing for an optional resource).
    """
    allowlisted = getattr(getattr(client, "session", None), "is_allowlisted_write", None)
    if (getattr(client, "read_only", False)
            and method.upper() not in ("GET", "HEAD", "OPTIONS")
            and not (callable(allowlisted) and allowlisted(method, url))):
        raise RuntimeError(
            f"BLOCKED: attempted {method} {url} on a read-only (source) account. "
            "Migrations only ever write to the destination.")
    key = _endpoint_key(url)
    elapsed = time.time() - _last_call.get(key, 0.0)
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)

    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    ensure = getattr(client, "ensure_token", None)

    # Retries cover 429 (with Retry-After), 5xx, and transport errors. That is
    # safe for the writes made through here because every one of them is an
    # upsert, a 409-tolerated create, or an idempotent invite/lock.
    response = None
    for attempt in range(max_attempts):
        # Re-resolve the token each attempt: backoffs can stack past the
        # expiry margin on long runs, and ensure_token re-authenticates then.
        token = ensure() if callable(ensure) else client.token
        headers["Authorization"] = f"Bearer {token}"

        try:
            response = client.session.request(method, url, headers=headers,
                                              json=json_data, params=params,
                                              timeout=60)
        except requests.exceptions.RequestException as e:
            if attempt == max_attempts - 1:
                raise
            wait = BACKOFF_SECONDS[attempt]
            log_warning(f"  {type(e).__name__} on {key} — retrying in {wait}s "
                        f"(attempt {attempt + 1}/{max_attempts})")
            time.sleep(wait)
            continue
        _last_call[key] = time.time()

        retryable = (response.status_code == 429
                     or (response.status_code >= 500
                         and response.status_code not in allow_status))
        if not retryable or attempt == max_attempts - 1:
            break
        try:
            wait = int(response.headers.get("Retry-After", "0")) or BACKOFF_SECONDS[attempt]
        except ValueError:
            wait = BACKOFF_SECONDS[attempt]
        log_warning(f"  {response.status_code} on {key} — backing off {wait}s "
                    f"(attempt {attempt + 1}/{max_attempts})")
        time.sleep(wait)

    if response.status_code in allow_status:
        return response
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        log_error(f"  {method} {key} failed [{response.status_code}]: {response.text[:300]}")
        raise
    return response


def get_all_users(client, tenant_id=None, page_size=200):
    """Every user in the environment (or one tenant), including tenants[]+roles.

    /identity/resources/users/v2 returns each user with a `tenants` array of
    {tenantId, roles[]} where roles are FULL role objects (id + key). v3 exists
    too, but on some stacks its tenants[] entries carry no roles at all, so v2
    is the portable choice; if a stack ever drops v2 we fall back to v3.
    Omitting frontegg-tenant-id scopes the call to the entire environment.

    The account-wide tenants[] is not guaranteed to be complete; pass the
    result through complete_memberships() before relying on it.
    """
    headers = {}
    if tenant_id:
        headers["frontegg-tenant-id"] = tenant_id

    users = []
    for version in ("v2", "v3"):
        url = f"{client.base_url}/identity/resources/users/{version}"
        users = []
        params = {"_limit": page_size}
        not_found = False
        while True:
            response = paced_request(client, "GET", url, headers=headers,
                                     params=params, allow_status=(404,),
                                     min_interval=INTERVAL_USER_LISTING if tenant_id else INTERVAL_GENERAL)
            if response.status_code == 404:
                not_found = True
                break
            payload = response.json()
            users.extend(payload.get("items", []))

            # `_offset` is a page index, not a record offset — only ever reuse
            # the value the server generated for us.
            next_link = (payload.get("_links") or {}).get("next")
            if not next_link:
                break
            offset = None
            for part in str(next_link).split("?")[-1].split("&"):
                if part.startswith("_offset="):
                    offset = part.split("=", 1)[1]
            if offset is None:
                break
            params = {"_limit": page_size, "_offset": offset}

        if not not_found:
            return users
        log_warning(f"  users/{version} not available on {client.base_url}; "
                    "trying the next version")
    return users


def get_all_roles(client, page_size=500):
    """Every role in the environment.

    NOTE: roles/v2 treats `_offset` as a RECORD offset (verified live: _limit=2
    &_offset=1 returns a window shifted by one record), even though its
    `_links.next` is generated with page arithmetic — so never follow those
    links here. Advance by the number of records received and stop on a short
    page. (users/v2-v3 and tenants/v2 are page-indexed instead; each fetcher
    matches its own endpoint's semantics.)
    """
    url = f"{client.base_url}/identity/resources/roles/v2"
    roles = []
    offset = 0
    while True:
        response = paced_request(client, "GET", url,
                                 params={"_limit": page_size, "_offset": offset})
        items = response.json().get("items", [])
        roles.extend(items)
        if len(items) < page_size:
            break
        offset += len(items)
    return roles


def build_role_maps(source_client, destination_client):
    """Role translation tables between the two environments.

    Role `id` is generated per environment so it never carries across accounts,
    and `name` carries no uniqueness guarantee. `key` is what lands in the JWT,
    but the same key may exist both account-wide and per tenant, so the scope has
    to be part of the match. Tenants keep their IDs across the migration, so a
    tenant-scoped source role maps to the same tenantId in the destination.

    Returns a dict with:
      id_map            source role id -> destination role id
      unmapped          source roles with no destination equivalent
      dest_by_scope_key (tenantId-or-GLOBAL_SCOPE, key) -> destination role id
      source_roles      the full source role list
    """
    return _build_role_maps(get_all_roles(source_client),
                            get_all_roles(destination_client))


def map_role_id(role, dest_by_scope_key):
    """Destination role id for one source role object (tenant scope first)."""
    key = role.get("key")
    scope = role.get("tenantId") or GLOBAL_SCOPE
    dest_id = dest_by_scope_key.get((scope, key))
    if dest_id is None and scope != GLOBAL_SCOPE:
        dest_id = dest_by_scope_key.get((GLOBAL_SCOPE, key))
    return dest_id


def build_role_map(source_client, destination_client):
    """Back-compat wrapper: returns (id_map, unmapped). See build_role_maps."""
    maps = build_role_maps(source_client, destination_client)
    return maps["id_map"], maps["unmapped"]


def _build_role_maps(source_roles, dest_roles):
    def scope_of(role):
        return role.get("tenantId") or GLOBAL_SCOPE

    dest_by_scope_key = {}
    for role in dest_roles:
        scope_key = (scope_of(role), role.get("key"))
        if scope_key in dest_by_scope_key:
            log_warning(f"  Duplicate destination role for scope/key {scope_key} — "
                        f"using the first match ({dest_by_scope_key[scope_key]}).")
            continue
        dest_by_scope_key[scope_key] = role["id"]

    mapping, unmapped = {}, []
    for role in source_roles:
        dest_id = dest_by_scope_key.get((scope_of(role), role.get("key")))
        if dest_id:
            mapping[role["id"]] = dest_id
        else:
            unmapped.append(role)

    logger.info(f"  Role map: {len(mapping)} of {len(source_roles)} source roles matched "
                f"({len(dest_roles)} roles in destination)")
    return {
        "id_map": mapping,
        "unmapped": unmapped,
        "dest_by_scope_key": dest_by_scope_key,
        "source_roles": source_roles,
    }


def index_users_by_email(users):
    """email (lowercased) -> user object. Later duplicates are ignored."""
    indexed = {}
    for user in users:
        email = (user.get("email") or "").strip().lower()
        if email and email not in indexed:
            indexed[email] = user
    return indexed


def memberships_of(user):
    """tenantId -> [roleId] for one user object from /users/v2 (or v3)."""
    out = {}
    for tenant in user.get("tenants") or []:
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        out[tenant_id] = [r.get("id") for r in (tenant.get("roles") or []) if r.get("id")]
    return out


def membership_role_objs(user):
    """tenantId -> [role object] for one user object from /users/v2.

    Role objects carry both `id` and `key`, so callers can translate to the
    destination by (scope, key) without relying on id continuity.
    """
    out = {}
    for tenant in user.get("tenants") or []:
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        out[tenant_id] = [r for r in (tenant.get("roles") or []) if isinstance(r, dict)]
    return out


USER_TENANT_ROLES_PATH = "/identity/resources/users/v3/roles"


def get_user_role_ids_on_tenant(client, user_id, tenant_id):
    """Role ids one user holds on one tenant (/users/v3/roles, tenant-scoped)."""
    response = paced_request(client, "GET", f"{client.base_url}{USER_TENANT_ROLES_PATH}",
                             headers={"frontegg-tenant-id": tenant_id},
                             params={"ids": user_id})
    for entry in response.json() or []:
        if entry.get("userId") == user_id:
            return [role_id for role_id in entry.get("roleIds") or [] if role_id]
    return []


def complete_memberships(client, users, tenant_ids, label="account"):
    """Fill in the memberships the account-wide user sweep leaves out.

    /users/v2 and /v3 return each user with tenants[]/tenantIds, but that array
    can omit memberships: seen on an EU account where accepted AND still-pending
    invitations from older invitation flows were missing from it, while the
    per-tenant listing (frontegg-tenant-id header) returned them. Comparing two
    sweeps therefore reports "in sync" for memberships that were never copied.

    This lists every tenant, and for each member missing from the user's
    tenants[] appends {tenantId, roles[]} with the role objects resolved from
    /users/v3/roles. Users are updated in place. Costs one listing per tenant
    plus one call per recovered membership. Returns the number recovered.
    """
    users_by_id = {user.get("id"): user for user in users if user.get("id")}
    roles_by_id = {role.get("id"): role for role in get_all_roles(client) if role.get("id")}
    recovered, unknown_members = 0, set()

    for tenant_id in tenant_ids:
        for member in get_all_users(client, tenant_id=tenant_id):
            user = users_by_id.get(member.get("id"))
            if user is None:
                unknown_members.add(member.get("email") or member.get("id"))
                continue
            known_tenants = {t.get("tenantId") for t in user.get("tenants") or []}
            if tenant_id in known_tenants:
                continue
            role_ids = get_user_role_ids_on_tenant(client, user["id"], tenant_id)
            role_objs = [roles_by_id[role_id] for role_id in role_ids if role_id in roles_by_id]
            user.setdefault("tenants", []).append({"tenantId": tenant_id, "roles": role_objs,
                                                   "recoveredFromTenantListing": True})
            if isinstance(user.get("tenantIds"), list) and tenant_id not in user["tenantIds"]:
                user["tenantIds"].append(tenant_id)
            recovered += 1

    if recovered:
        log_warning(f"  {label}: {recovered} memberships were missing from the account-wide "
                    "user listing and were recovered from the per-tenant listings")
    if unknown_members:
        log_warning(f"  {label}: {len(unknown_members)} tenant members are not in the "
                    "account-wide user listing at all (ignored)")
    return recovered


# Server-generated fields that must never be replayed into a create/update body.
SERVER_FIELDS = ("id", "_id", "createdAt", "updatedAt", "vendorId", "clientId", "__v")


def strip_server_fields(obj, extra=()):
    """Copy of a dict without server-generated fields (non-recursive)."""
    if not isinstance(obj, dict):
        return obj
    drop = set(SERVER_FIELDS) | set(extra)
    return {k: v for k, v in obj.items() if k not in drop}
