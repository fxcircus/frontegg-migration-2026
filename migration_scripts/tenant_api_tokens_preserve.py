"""Migrate tenant API tokens WITH their original clientId and secret (preserve mode).

Selected with TENANT_API_TOKENS_MODE=preserve. The default mode, recreate
(tenant_api_tokens.py), issues every token a NEW clientId and secret instead.

How it works
------------
Frontegg's tenant API token export/import endpoints carry a token across
environments together with its stored (bcrypt) secret hash, so the clientId +
secret the customer already holds keep working against the destination. These
endpoints are not in Frontegg's public API reference, and Frontegg turns them on
per environment with the feature flag `tenant-api-token-export-import-enabled`.
The flag must be on for BOTH environments; otherwise every call returns
403 ER-01247. If the flag is off this step stops before writing anything. It
never falls back to issuing new credentials.

  1. Export every tenant API token from the source and from the destination.
  2. Skip what should not be imported: tokens already in the destination (same
     clientId, so re-runs are safe), expired tokens (unless
     MIGRATE_EXPIRED_API_TOKENS=true), federated tokens, tokens whose tenant is
     missing in the destination, and tokens the import would reject as
     malformed.
  3. COPY the rest into the destination, up to 25 per import call. The source
     keeps its token. The import validates each token's tenant, roles and
     permissions before it stores anything, so a token it rejects is reported
     and nothing is written for it.
  4. MOVE, same region only. A clientId is unique across every environment in a
     region, so while the source still holds a token the copy is rejected with
     409 ER-01244. That rejection comes only after the tenant, role and
     permission checks passed, so these tokens are known to be importable.
     They are left untouched and listed unless
     TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true, in which case each one is
     backed up to TENANT_API_TOKENS_BACKUP_DIR, deleted from the source,
     imported into the destination, and re-imported into the source if the
     destination import keeps failing. The token works nowhere for the few
     seconds between the delete and the import. This is the ONLY write this
     tool ever makes to a source account.

Not carried over: user API tokens (MIGRATE_USER_API_TOKENS still recreates
them with new credentials) and private_key_jwt public keys (re-register them in
the destination). The import does not fire the frontegg.tenantApiToken.created
webhook; the source delete of a moved token DOES fire the source's
frontegg.tenantApiToken.deleted webhook.
"""

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from utility.frontegg_client import FronteggClient
from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, get_all_roles,
                                   paced_request)

logger = get_logger()

FEATURE_FLAG = "tenant-api-token-export-import-enabled"

EXPORT_PATH = "/identity/resources/tenants/api-tokens/v1/export"
IMPORT_PATH = "/identity/resources/tenants/api-tokens/v1/import"
DELETE_PATH = "/identity/resources/tenants/api-tokens/v1/{}"

EXPORT_PAGE_SIZE = 500   # the most the export returns per call
IMPORT_BATCH_SIZE = 25   # the most the import accepts per call
INTERVAL_EXPORT = 6.5    # export is limited to 10 calls/min per environment
MOVE_IMPORT_ATTEMPTS = 3
MOVE_RETRY_SECONDS = 10

# Fields the import accepts, copied from the export record. Tenants keep their
# IDs across the migration, so tenantId is copied as is.
IMPORT_FIELDS = ("clientId", "tenantId", "secretHash", "description", "metadata",
                 "roleKeys", "permissionKeys", "expires", "createdAt", "createdByUserId")

FLAG_OFF = "ER-01247"
CLIENT_ID_TAKEN = "ER-01244"
TENANT_LOOKUP_FAILED = "ER-01064"

# The only writes the source may ever receive, and only through the separate
# client built when TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true: deleting one
# tenant API token, and re-importing it if the destination import then fails.
SOURCE_MOVE_WRITES = (
    ("DELETE", re.compile(r"/identity/resources/tenants/api-tokens/v1/(?!import$|export$)[^/]+")),
    ("POST", re.compile(r"/identity/resources/tenants/api-tokens/v1/import")),
)

# The import's own validation rules. A token that breaks any of them makes the
# import reject its WHOLE batch with 400, so they are checked up front.
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
BCRYPT = re.compile(r"\$2[aby]\$(0[4-9]|[12]\d|3[01])\$[./A-Za-z0-9]{53}")
MAX_KEY_LENGTH = 255

HINTS = {
    FLAG_OFF: f"The '{FEATURE_FLAG}' feature flag is off for this environment. "
              "Ask Frontegg to enable it (it is set per environment).",
    CLIENT_ID_TAKEN: "This clientId already exists in another environment in the "
                     "destination's region.",
    "ER-01245": "A role or permission key does not exist in the destination (for "
                "roles: account-level or on the token's tenant). Migrate roles and "
                "permissions first.",
    "ER-01246": "A role or permission key matches more than one role or permission "
                "in the destination (e.g. the same role key account-level AND on "
                "the token's tenant).",
    "ER-01099": "The token's tenant does not exist in the destination.",
    "Api tokens are disabled": "API tokens are disabled in this environment, or (import "
                               "only) its machine-to-machine strategy is not Client "
                               "Credentials.",
}


class FeatureFlagOff(Exception):
    """The export/import endpoints are not enabled for an environment."""


def _env_true(name):
    return os.getenv(name, "False").lower() == "true"


def _backup_dir():
    return os.getenv("TENANT_API_TOKENS_BACKUP_DIR", "tenant_api_token_backups")


def _scrub(text):
    """Strip anything secret-shaped (bcrypt hashes, JWTs) from text about to be logged."""
    text = re.sub(r"\$2[aby]?\$\d{2}\$[./A-Za-z0-9]{53}", "<bcrypt hash>", str(text))
    return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<jwt>", text)


def _hint(text):
    return next((hint for needle, hint in HINTS.items() if needle in text), None)


def _label(token):
    description = token.get("description") or "(no description)"
    return f"{token.get('tenantId')}: {description!r} [{token.get('clientId')}]"


def _log_failure(token, outcome):
    log_error(f"  {_label(token)}: import failed [{outcome['status']}] {outcome['error']}")
    hint = _hint(outcome["error"])
    if hint:
        log_error(f"    -> {hint}")


def _parse_date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expired(token):
    expires = _parse_date(token["expires"]) if token.get("expires") else None
    return expires is not None and expires <= datetime.now(timezone.utc)


def _is_federated(token):
    """Federated tokens have a UUIDv5 clientId derived from the vendor id.

    Login re-derives that id from the DESTINATION's vendor id, so an imported
    federated token is never found there.
    """
    match = UUID.fullmatch(str(token.get("clientId") or ""))
    return bool(match) and match.group(1) == "5"


def _shape_problems(token):
    """Where this token breaks the import's validation rules (see UUID/BCRYPT above)."""
    problems = []
    if not UUID.fullmatch(str(token.get("clientId") or "")):
        problems.append("clientId is not a UUID")
    tenant_id = token.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > MAX_KEY_LENGTH:
        problems.append("tenantId is empty or too long")
    if not BCRYPT.fullmatch(str(token.get("secretHash") or "")):
        problems.append("secretHash is not a bcrypt hash")
    if token.get("metadata") is not None and not isinstance(token["metadata"], dict):
        problems.append("metadata is not a JSON object")
    if token.get("description") is not None and not isinstance(token["description"], str):
        problems.append("description is not a string")
    for field in ("roleKeys", "permissionKeys"):
        keys = token.get(field)
        if keys is not None and not (isinstance(keys, list) and all(
                isinstance(k, str) and len(k) <= MAX_KEY_LENGTH for k in keys)):
            problems.append(f"{field} is not a list of keys")
    for field in ("expires", "createdAt"):
        if token.get(field) is not None and _parse_date(token[field]) is None:
            problems.append(f"{field} is not a date")
    created_by = token.get("createdByUserId")
    if created_by is not None and not (isinstance(created_by, str)
                                       and len(created_by) <= MAX_KEY_LENGTH):
        problems.append("createdByUserId is too long")
    return problems


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_tokens(client, label, tenant_id=None):
    """Every tenant API token in one environment (or one tenant), secret hashes included.

    Ordered by (createdAt, id). Raises FeatureFlagOff when the environment does
    not have the endpoints enabled.
    """
    url = f"{client.base_url}{EXPORT_PATH}"
    params = {"_limit": EXPORT_PAGE_SIZE, "_offset": 0}
    if tenant_id:
        params["tenantId"] = tenant_id
    tokens = []
    while True:
        response = paced_request(client, "GET", url, params=params,
                                 min_interval=INTERVAL_EXPORT,
                                 allow_status=(400, 401, 403, 404))
        if response.status_code == 404 or (response.status_code == 403
                                           and FLAG_OFF in response.text):
            raise FeatureFlagOff(label)
        if response.status_code != 200:
            text = _scrub(response.text[:300])
            raise RuntimeError(f"{label} export failed [{response.status_code}]: {text} "
                               f"{_hint(text) or ''}")
        page = response.json()
        tokens.extend(page)
        if len(page) < EXPORT_PAGE_SIZE:
            return tokens
        # `_offset` is the number of records to skip, not a page number.
        params["_offset"] += len(page)


def _held_by(client, label, tokens):
    """The clientIds of `tokens` that the environment holds right now."""
    if len(tokens) == 1:
        exported = export_tokens(client, label, tenant_id=tokens[0]["tenantId"])
    else:
        exported = export_tokens(client, label)
    present = {t.get("clientId") for t in exported}
    return {t["clientId"] for t in tokens if t["clientId"] in present}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _import(client, tokens):
    """One import call. Returns (request status, {clientId: outcome}).

    An outcome is {"imported", "status", "error", "token"}. The import answers
    200 even when some tokens fail, so each token's own result is read. 5xx is
    never retried here: tokens before the failure may already be stored, so the
    caller checks the destination first. Their status is None ("unknown").
    """
    url = f"{client.base_url}{IMPORT_PATH}"
    body = {"tokens": [{k: t[k] for k in IMPORT_FIELDS if t.get(k) is not None}
                       for t in tokens]}
    try:
        response = paced_request(client, "POST", url, json_data=body,
                                 min_interval=INTERVAL_GENERAL,
                                 allow_status=tuple(range(400, 600)))
    except Exception as e:
        return None, {t["clientId"]: {"imported": False, "status": None,
                                      "error": _scrub(e), "token": None} for t in tokens}

    if response.status_code != 200:
        status = None if response.status_code >= 500 else response.status_code
        error = _scrub(response.text[:300])
        return response.status_code, {t["clientId"]: {"imported": False, "status": status,
                                                      "error": error, "token": None}
                                      for t in tokens}

    results = {r.get("clientId"): r for r in (response.json().get("results") or [])}
    outcomes = {}
    for token in tokens:
        result = results.get(token["clientId"]) or {}
        error = " ".join(str(part) for part in (result.get("errorCode"),
                                                result.get("errorMessages")) if part)
        status = result.get("status")
        if not result or (isinstance(status, int) and status >= 500) \
                or TENANT_LOOKUP_FAILED in error:
            status = None  # transient: the tenants lookup or the server failed
        outcomes[token["clientId"]] = {"imported": bool(result.get("imported")),
                                       "status": status,
                                       "error": _scrub(error or "no result returned"),
                                       "token": result.get("token")}
    return 200, outcomes


def _copy(destination_client, tokens):
    """Import tokens into the destination in batches. Returns {clientId: outcome}."""
    outcomes = {}
    for start in range(0, len(tokens), IMPORT_BATCH_SIZE):
        batch = tokens[start:start + IMPORT_BATCH_SIZE]
        status, result = _import(destination_client, batch)
        if status in (401, 403):
            # Every later batch would be refused the same way, so stop here.
            refused = result[batch[0]["clientId"]]
            log_error(f"  The destination refused the import [{status}] {refused['error']}")
            hint = _hint(refused["error"])
            if hint:
                log_error(f"    -> {hint}")
            for token in tokens[start:]:
                outcomes[token["clientId"]] = dict(refused, refused=True)
            break
        if status == 400 and len(batch) > 1:
            # One invalid token rejects the whole batch; isolate it.
            for token in batch:
                result.update(_import(destination_client, [token])[1])
        outcomes.update(result)
    return outcomes


def _check_role_scopes(token, imported, source_roles, destination_roles_by_id):
    """Warn when a role key was bound to a different scope than it had in the source.

    The import resolves a role key against account-level roles AND the token's
    tenant's roles, and binds whichever single role matches, so a tenant-scoped
    source role can silently land on an account-level destination role.
    """
    tenant_id = token["tenantId"]
    for role_id in (imported or {}).get("roleIds") or []:
        role = destination_roles_by_id.get(role_id)
        if not role:
            continue
        key = role.get("key")
        source_scope = tenant_id if (tenant_id, key) in source_roles else None
        if (role.get("tenantId") or None) != source_scope:
            where = "an account-level" if not role.get("tenantId") else "a tenant-scoped"
            log_warning(f"  {_label(token)}: role {key!r} was bound to {where} role in the "
                        "destination, but the source token used the other scope")


# ---------------------------------------------------------------------------
# Move (same region, opt-in)
# ---------------------------------------------------------------------------

def _save_backup(token, base_url):
    """Write the exported record (secret hash included) before deleting it from the source."""
    directory = _backup_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(directory, f"{token['clientId']}-{stamp}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    body = {"tokens": [{k: token[k] for k in IMPORT_FIELDS if token.get(k) is not None}]}
    with os.fdopen(fd, "w") as handle:
        json.dump({"exportedFrom": base_url, "importBody": body}, handle, indent=2)
    return path


def _delete_from_source(writer, token):
    """Delete one token from the source. Returns True once the source no longer holds it.

    Any answer other than 200/204 (a 5xx, or a 404 after a retried request) may
    still mean the token is gone, so the source itself is the judge.
    """
    try:
        response = paced_request(writer, "DELETE",
                                 f"{writer.base_url}{DELETE_PATH.format(token['clientId'])}",
                                 headers={"frontegg-tenant-id": token["tenantId"]},
                                 allow_status=tuple(range(400, 600)))
        if response.status_code in (200, 204):
            return True
        answer = f"[{response.status_code}] {_scrub(response.text[:200])}"
    except Exception as e:
        answer = _scrub(e)
    if not _held_by(writer, "source", [token]):
        return True
    log_error(f"  {_label(token)}: delete from source failed {answer} - nothing was changed")
    return False


def _move(writer, destination_client, token):
    """Back up, delete from the source, import into the destination, roll back on failure.

    Returns (result, outcome): result is one of moved, delete-failed,
    rolled-back, lost.
    """
    backup = _save_backup(token, writer.base_url)
    if not _delete_from_source(writer, token):
        return "delete-failed", None

    # The copy attempt already passed the tenant/role/permission checks, so a
    # failure here is expected to be transient: retry before rolling back.
    for attempt in range(MOVE_IMPORT_ATTEMPTS):
        outcome = _import(destination_client, [token])[1][token["clientId"]]
        if outcome["imported"]:
            log_success(f"  {_label(token)}: moved")
            return "moved", outcome
        transient = outcome["status"] is None or CLIENT_ID_TAKEN in outcome["error"]
        if transient and _held_by(destination_client, "destination", [token]):
            log_success(f"  {_label(token)}: moved (confirmed in the destination)")
            return "moved", None
        if not transient:
            break  # a definite rejection; retrying will not change it
        if attempt < MOVE_IMPORT_ATTEMPTS - 1:
            time.sleep(MOVE_RETRY_SECONDS)

    _log_failure(token, outcome)
    restored = _import(writer, [token])[1][token["clientId"]]
    if restored["imported"]:
        log_warning(f"  {_label(token)}: rolled back - the token is back in the source, "
                    "unchanged")
        return "rolled-back", None
    if _held_by(destination_client, "destination", [token]):
        log_success(f"  {_label(token)}: moved (confirmed in the destination)")
        return "moved", None
    if _held_by(writer, "source", [token]):
        log_warning(f"  {_label(token)}: rolled back (confirmed in the source)")
        return "rolled-back", None
    log_error(f"  {_label(token)}: ROLLBACK FAILED [{restored['status']}] {restored['error']}. "
              f"The token now exists in NEITHER environment. Restore it by POSTing the "
              f"'importBody' from {backup} to {IMPORT_PATH} (see README, section 9).")
    return "lost", None


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------

def migrate_tenant_api_tokens_preserving(source_client, destination_client):
    log_section("Tenant API Tokens (preserve original clientId + secret)")

    from migration_scripts.tenants import get_tenants

    include_expired = _env_true("MIGRATE_EXPIRED_API_TOKENS")
    allow_source_delete = _env_true("TENANT_API_TOKENS_ALLOW_SOURCE_DELETE")
    # Environments behind the same regional API share one token table, where the
    # clientId is the primary key. That is what makes a copy impossible.
    same_region = (urlparse(source_client.base_url).netloc.lower()
                   == urlparse(destination_client.base_url).netloc.lower())

    try:
        source_tokens = export_tokens(source_client, "source")
        destination_tokens = export_tokens(destination_client, "destination")
    except FeatureFlagOff as e:
        log_error(f"Tenant API token export/import is not enabled on the {e} environment. "
                  f"Ask Frontegg to enable the '{FEATURE_FLAG}' feature flag on BOTH "
                  "environments, or set TENANT_API_TOKENS_MODE=recreate to issue new "
                  "credentials instead. Nothing was changed.")
        return

    logger.info(f"Source holds {len(source_tokens)} tenant API tokens; the destination "
                f"holds {len(destination_tokens)}.")
    if same_region:
        logger.info("Source and destination share a region, so tokens can only be MOVED "
                    "(deleted from the source), not copied.")

    destination_tenants = {t.get("tenantId") or t.get("id")
                           for t in get_tenants(destination_client) or []}
    source_roles = {(r.get("tenantId"), r.get("key")) for r in get_all_roles(source_client)
                    if r.get("tenantId")}
    destination_roles_by_id = {r["id"]: r for r in get_all_roles(destination_client)}

    in_destination = {t.get("clientId") for t in destination_tokens}
    source_ids = {t.get("clientId") for t in source_tokens}
    # Destination tokens with a clientId the source does not hold, keyed by tenant
    # and description. Either recreated by an earlier recreate-mode run or moved
    # by an earlier preserve-mode run; the two cannot be told apart, so this only
    # warns.
    other_tokens = {(t.get("tenantId"), (t.get("description") or "").strip())
                    for t in destination_tokens if t.get("clientId") not in source_ids}

    counts = Counter()
    to_copy = []
    for token in source_tokens:
        problems = _shape_problems(token)
        if token.get("clientId") in in_destination:
            counts["already in destination"] += 1
        elif _expired(token) and not include_expired:
            counts["skipped: expired"] += 1
        elif _is_federated(token):
            log_warning(f"  {_label(token)}: skipped - federated token; its clientId is "
                        "derived from the source environment, so it cannot work in another "
                        "environment")
            counts["skipped: federated"] += 1
        elif problems:
            log_error(f"  {_label(token)}: skipped - the import would reject it: "
                      f"{'; '.join(problems)}")
            counts["skipped: invalid"] += 1
        elif token.get("tenantId") not in destination_tenants:
            log_error(f"  {_label(token)}: skipped - its tenant does not exist in the "
                      "destination (run MIGRATE_TENANTS first)")
            counts["skipped: tenant missing"] += 1
        else:
            description = (token.get("description") or "").strip()
            if description and (token["tenantId"], description) in other_tokens:
                log_warning(f"  {_label(token)}: the destination tenant also has a token "
                            "with this description but a different clientId. If that one "
                            "was recreated by an earlier recreate-mode run, delete it once "
                            "consumers use the original credentials.")
            to_copy.append(token)

    # Copy pass, plus one retry for tokens whose outcome was unknown.
    outcomes = _copy(destination_client, to_copy)
    unknown = [t for t in to_copy if outcomes[t["clientId"]]["status"] is None
               and not outcomes[t["clientId"]]["imported"]]
    if unknown:
        landed = _held_by(destination_client, "destination", unknown)
        for token in unknown:
            if token["clientId"] in landed:
                outcomes[token["clientId"]] = {"imported": True, "status": 200,
                                               "error": "", "token": None}
        outcomes.update(_copy(destination_client,
                              [t for t in unknown if t["clientId"] not in landed]))

    # A 409 can also mean an earlier attempt in this run stored the token after
    # all, so check the destination before treating it as "held by the source".
    taken = [t for t in to_copy if CLIENT_ID_TAKEN in outcomes[t["clientId"]]["error"]]
    landed = _held_by(destination_client, "destination", taken) if taken else set()
    taken_ids = {t["clientId"] for t in taken}

    collisions = []
    for token in to_copy:
        outcome = outcomes[token["clientId"]]
        if outcome["imported"] or token["clientId"] in landed:
            counts["copied"] += 1
            log_success(f"  {_label(token)}: copied")
            _check_role_scopes(token, outcome["token"], source_roles, destination_roles_by_id)
        elif token["clientId"] in taken_ids:
            collisions.append(token)
        elif outcome.get("refused"):
            counts["failed: import refused"] += 1
        else:
            counts["failed"] += 1
            _log_failure(token, outcome)

    if collisions and not same_region:
        # Something else in the destination's region holds these clientIds;
        # deleting the source token cannot free them.
        counts["failed: clientId taken"] += len(collisions)
        log_error(f"{len(collisions)} tokens were rejected because their clientId already "
                  "exists in another environment in the destination's region. Nothing was "
                  "changed for them.")
        for token in collisions:
            logger.info(f"  {_label(token)}")
    elif collisions and not allow_source_delete:
        counts["needs a move"] += len(collisions)
        log_warning(f"{len(collisions)} tokens passed every destination check but are still "
                    "held by the source, so they can only be MOVED. Nothing was changed for "
                    "them. Set TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true and re-run to move "
                    "them (each is backed up, deleted from the source, then imported).")
        for token in collisions:
            logger.info(f"  {_label(token)}")
    elif collisions:
        log_warning(f"Moving {len(collisions)} tokens: each is backed up to {_backup_dir()}/, "
                    "DELETED FROM THE SOURCE, then imported into the destination.")
        writer = FronteggClient(source_client.base_url, source_client.client_id,
                                source_client.secret, read_only=True,
                                write_allowlist=SOURCE_MOVE_WRITES)
        for token in collisions:
            result, outcome = _move(writer, destination_client, token)
            counts[result] += 1
            if outcome:
                _check_role_scopes(token, outcome["token"], source_roles,
                                   destination_roles_by_id)
        log_warning(f"Backups of the moved tokens are in {_backup_dir()}/ (mode 0600). They "
                    "hold secret hashes: delete them once the migration is confirmed.")

    logger.print_stats("Tenant API tokens (preserve)", dict(counts))
    if counts["lost"]:
        log_error(f"{counts['lost']} tokens exist in NEITHER environment - restore them from "
                  f"{_backup_dir()}/ now (see the errors above and README section 9).")
    if counts["copied"] or counts["moved"]:
        log_warning("Migrated tokens keep their clientId + secret, but consumers must now "
                    "exchange them at the DESTINATION environment's domain; the JWTs they "
                    "get are signed with the destination's keys.")
    if counts["copied"] and not same_region:
        log_warning("Copied tokens still work in the source too. Delete them there once "
                    "consumers have switched to the destination.")
