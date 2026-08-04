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

import time
from urllib.parse import urlparse

import requests

from utility.logger import get_logger, log_error, log_warning

logger = get_logger()

# Documented Frontegg limits, expressed as the minimum seconds between calls.
# Launch-tier values so the defaults are safe on every plan; override per call.
INTERVAL_BULK_INVITE = 12.0   # 5/min per environment  -> 1 per 12s
INTERVAL_TENANTS = 2.0        # 30/min per environment -> 1 per 2s
INTERVAL_GENERAL = 0.6        # 100/min per IP         -> 1 per 0.6s

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = [30, 60, 120, 240]

GLOBAL_SCOPE = "__GLOBAL__"

_last_call = {}


def _endpoint_key(url):
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def paced_request(client, method, url, headers=None, json_data=None, params=None,
                  min_interval=INTERVAL_GENERAL, max_attempts=MAX_ATTEMPTS,
                  allow_status=()):
    """Make a request, pacing by endpoint and backing off through 429s.

    `allow_status` lists non-2xx codes to return instead of raising (e.g. 404
    when probing for an optional resource).
    """
    key = _endpoint_key(url)
    elapsed = time.time() - _last_call.get(key, 0.0)
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)

    headers = dict(headers or {})
    ensure = getattr(client, "ensure_token", None)
    token = ensure() if callable(ensure) else client.token
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json")

    response = None
    for attempt in range(max_attempts):
        response = client.session.request(method, url, headers=headers,
                                          json=json_data, params=params, timeout=60)
        _last_call[key] = time.time()

        if response.status_code != 429:
            break
        if attempt == max_attempts - 1:
            break
        try:
            wait = int(response.headers.get("Retry-After", "0")) or BACKOFF_SECONDS[attempt]
        except ValueError:
            wait = BACKOFF_SECONDS[attempt]
        log_warning(f"  429 on {key} — backing off {wait}s "
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

    /identity/resources/users/v3 returns each user with a `tenants` array of
    {tenantId, roles[]}, so a single sweep yields the whole membership graph.
    Omitting frontegg-tenant-id scopes the call to the entire environment.
    """
    url = f"{client.base_url}/identity/resources/users/v3"
    headers = {}
    if tenant_id:
        headers["frontegg-tenant-id"] = tenant_id

    users = []
    params = {"_limit": page_size}
    while True:
        response = paced_request(client, "GET", url, headers=headers, params=params)
        payload = response.json()
        users.extend(payload.get("items", []))

        # `_offset` is a page index, not a record offset — only ever reuse the
        # value the server generated for us.
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

    return users


def build_role_map(source_client, destination_client):
    """Map source role ID -> destination role ID, matched on (tenant scope, key).

    Role `id` is generated per environment so it never carries across accounts,
    and `name` carries no uniqueness guarantee. `key` is what lands in the JWT,
    but the same key may exist both account-wide and per tenant, so the scope has
    to be part of the match. Tenants keep their IDs across the migration, so a
    tenant-scoped source role maps to the same tenantId in the destination.

    Returns (mapping, unmapped) where unmapped is a list of source roles with no
    destination equivalent.
    """
    def fetch(client):
        url = f"{client.base_url}/identity/resources/roles/v2"
        response = paced_request(client, "GET", url, params={"_limit": 2000})
        return response.json().get("items", [])

    def scope_of(role):
        return role.get("tenantId") or GLOBAL_SCOPE

    source_roles = fetch(source_client)
    dest_roles = fetch(destination_client)

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
    return mapping, unmapped


def index_users_by_email(users):
    """email (lowercased) -> user object. Later duplicates are ignored."""
    indexed = {}
    for user in users:
        email = (user.get("email") or "").strip().lower()
        if email and email not in indexed:
            indexed[email] = user
    return indexed


def memberships_of(user):
    """tenantId -> [roleId] for one user object from /users/v3."""
    out = {}
    for tenant in user.get("tenants") or []:
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        out[tenant_id] = [r.get("id") for r in (tenant.get("roles") or []) if r.get("id")]
    return out
