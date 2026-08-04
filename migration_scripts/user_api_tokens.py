"""Migrate user API tokens (personal client-credentials tokens).

Same caveat as tenant API tokens, and for the same reason: clientId and secret
are generated server-side and the secret is returned exactly once, at creation.
Nothing in the public API accepts an existing credential pair, so every migrated
token is NEW and each owner has to re-point whatever uses it. New credentials are
written to a handover CSV as they are created.

Two differences from tenant tokens worth knowing:

* User tokens carry no roleIds. Authorization is inherited from the roles the
  user holds on the tenant the token is scoped to, so run the user membership and
  role migration first or the new token will resolve to the wrong permissions.
* Reading them is expensive. The list endpoint requires BOTH frontegg-tenant-id
  and frontegg-user-id, and there is no cross-user or cross-tenant listing, so
  discovery costs one call per (user, tenant) membership. On a large account that
  is thousands of calls; this step is off by default for that reason.
"""

import csv
import os
from datetime import datetime, timezone

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, get_all_users,
                                   index_users_by_email, memberships_of,
                                   paced_request)

logger = get_logger()

TOKENS_PATH = "/identity/resources/users/api-tokens/v1"
# Spec bound on expiresInMinutes for user tokens: 1 .. 5256000 (10 years).
MAX_EXPIRES_IN_MINUTES = 5256000

OUTPUT_FIELDS = ["email", "tenantId", "description", "newClientId", "newSecret",
                 "expiresInMinutes", "sourceClientId", "createdAt"]


def _output_path():
    return os.getenv("USER_API_TOKENS_OUTPUT_CSV", "migrated_user_api_tokens.csv")


def _list_tokens(client, tenant_id, user_id):
    response = paced_request(
        client, "GET", f"{client.base_url}{TOKENS_PATH}",
        headers={"frontegg-tenant-id": tenant_id, "frontegg-user-id": user_id},
        min_interval=INTERVAL_GENERAL, allow_status=(400, 403, 404),
    )
    if response.status_code >= 400:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    # This route returns a bare array, unlike the access-tokens sibling.
    return payload if isinstance(payload, list) else payload.get("items", [])


def _minutes_until(expires):
    if not expires:
        return None
    try:
        parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    minutes = int((parsed - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes < 1:
        return None
    return min(minutes, MAX_EXPIRES_IN_MINUTES)


def migrate_user_api_tokens(source_client, destination_client):
    log_section("User API Tokens")
    log_warning("Migrated tokens receive NEW clientId and secret values — the originals "
                "cannot be carried over. Owners must be re-pointed.")

    include_expired = os.getenv("MIGRATE_EXPIRED_API_TOKENS", "False").lower() == "true"

    logger.info("Fetching users from both accounts...")
    source_users = get_all_users(source_client)
    dest_by_email = index_users_by_email(get_all_users(destination_client))
    logger.info(f"  {len(source_users)} source users, {len(dest_by_email)} destination users")

    pairs = []
    for user in source_users:
        email = (user.get("email") or "").strip().lower()
        user_id = user.get("id")
        if not email or not user_id:
            continue
        for tenant_id in memberships_of(user):
            pairs.append((email, user_id, tenant_id))

    if not pairs:
        log_warning("No user/tenant memberships found in the source; nothing to scan.")
        return

    logger.info(f"Scanning {len(pairs)} (user, tenant) pairs for personal API tokens. "
                "There is no bulk listing endpoint, so this is one call per pair.")

    output_path = _output_path()
    fresh = not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    created = skipped = failed = 0
    found = 0

    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        if fresh:
            writer.writeheader()
            handle.flush()

        for email, source_user_id, tenant_id in pairs:
            source_tokens = _list_tokens(source_client, tenant_id, source_user_id)
            if not source_tokens:
                continue
            found += len(source_tokens)

            dest_user = dest_by_email.get(email)
            if not dest_user or not dest_user.get("id"):
                log_warning(f"  {email}: exists in source with {len(source_tokens)} token(s) "
                            "but not in destination — skipped")
                skipped += len(source_tokens)
                continue
            dest_user_id = dest_user["id"]

            if tenant_id not in memberships_of(dest_user):
                log_warning(f"  {email}: not a member of {tenant_id} in the destination — "
                            "run the user membership migration first")
                skipped += len(source_tokens)
                continue

            existing = {(t.get("description") or "").strip()
                        for t in _list_tokens(destination_client, tenant_id, dest_user_id)}

            for token in source_tokens:
                description = (token.get("description") or "").strip()
                expires_in = _minutes_until(token.get("expires"))
                if token.get("expires") and expires_in is None and not include_expired:
                    skipped += 1
                    continue
                if description and description in existing:
                    skipped += 1
                    continue

                body = {"description": description,
                        "metadata": token.get("metadata") or {}}
                if expires_in:
                    body["expiresInMinutes"] = expires_in

                try:
                    response = paced_request(
                        destination_client, "POST",
                        f"{destination_client.base_url}{TOKENS_PATH}",
                        headers={"frontegg-tenant-id": tenant_id,
                                 "frontegg-user-id": dest_user_id},
                        json_data=body, min_interval=INTERVAL_GENERAL,
                    )
                    payload = response.json()
                except Exception as e:
                    failed += 1
                    log_error(f"  {email} @ {tenant_id}: failed to create "
                              f"{description!r}: {e}")
                    continue

                # Written immediately: the secret is shown exactly once.
                writer.writerow({
                    "email": email,
                    "tenantId": tenant_id,
                    "description": description,
                    "newClientId": payload.get("clientId", ""),
                    "newSecret": payload.get("secret", ""),
                    "expiresInMinutes": body.get("expiresInMinutes", ""),
                    "sourceClientId": token.get("clientId", ""),
                    "createdAt": payload.get("createdAt", ""),
                })
                handle.flush()
                os.fsync(handle.fileno())
                created += 1
                log_success(f"  {email} @ {tenant_id}: created {description!r} -> "
                            f"{payload.get('clientId')}")

    logger.info(f"Source tokens found: {found}")
    log_success(f"Tokens created: {created}")
    if skipped:
        logger.info(f"Skipped (already present, expired, or user/membership missing): {skipped}")
    if failed:
        log_error(f"Failed: {failed}")
    if created:
        log_warning(f"NEW SECRETS written to {output_path} (mode 0600). Share them over a "
                    "secure channel and delete the local copy when done.")
