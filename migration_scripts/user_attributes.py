"""Migrate per-user attributes onto users that already exist in the destination.

This step does NOT create users. It runs after user migration and carries over
the attributes the public API can both read and write, closing gaps like "locked
users arrived unlocked". Concretely:

  1. One sweep of /identity/resources/users/v2 per account; source users are
     matched to destination users by email.
  2. PUT /identity/resources/users/v1/{id} (vendor scope) updates the writable
     fields: name, phoneNumber, profilePictureUrl, metadata, vendorMetadata,
     externalId. Only fields that are non-empty in the source AND differ from
     the destination are sent — destination data is never nulled out.
  3. profilePictureUrl is only accepted when its domain is on the destination
     environment's allowed-domains list; a 400 naming the profile picture makes
     us retry the same update without it so the other fields still land.
  4. POST .../users/v1/{id}/lock re-locks users who are locked in the source.
     Unlocking (locked only in the destination) is destructive-ish, so it only
     happens when USER_ATTRIBUTES_UNLOCK=true; otherwise it is reported.

Attributes the public API reads but cannot write — mfaEnrolled, verified,
provider, managedBy — are compared and reported as drift; fixing those (like MFA
secrets and password state, which are not readable at all) needs Frontegg Support.
"""

import json
import os

from utility.logger import (get_logger, log_error, log_section, log_success,
                            log_warning)
from utility.migration_api import (INTERVAL_GENERAL, get_all_users,
                                   index_users_by_email, paced_request)

logger = get_logger()

USER_PATH = "/identity/resources/users/v1"

# Fields PUT /identity/resources/users/v1/{id} accepts (verified live).
WRITABLE_FIELDS = ("name", "phoneNumber", "profilePictureUrl", "metadata",
                   "vendorMetadata", "externalId")
# Stringified-JSON fields where "", "{}" and "null" all mean "nothing there".
JSON_STRING_FIELDS = ("metadata", "vendorMetadata")


def _is_meaningful(field, value):
    """True when a source value is worth writing (never null-out destination data)."""
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        if field in JSON_STRING_FIELDS and stripped in ("{}", "null"):
            return False
    return True


def _values_differ(field, source_value, dest_value):
    """Compare one field across accounts; JSON-string fields compare parsed."""
    if field in JSON_STRING_FIELDS:
        try:
            return json.loads(source_value) != json.loads(dest_value or "null")
        except (TypeError, ValueError):
            pass  # not parseable on one side — fall back to string compare
    return source_value != dest_value


def _read_only_drift(source_user, dest_user):
    """Fields the public API cannot write where the two accounts disagree."""
    diffs = []
    for field in ("mfaEnrolled", "verified"):
        if bool(source_user.get(field)) != bool(dest_user.get(field)):
            diffs.append(field)
    for field in ("provider", "managedBy"):
        source_value = source_user.get(field)
        if source_value is not None and source_value != dest_user.get(field):
            diffs.append(field)
    return diffs


def _update_user(client, user_id, body, interval):
    """PUT the changed fields; returns (updated, picture_restricted).

    A 400 naming the profile picture means the URL's domain is not on the
    destination environment's allowed-domains list. Retry once without the
    picture so the remaining fields still land.
    """
    url = f"{client.base_url}{USER_PATH}/{user_id}"
    allow = (400,) if "profilePictureUrl" in body else ()
    response = paced_request(client, "PUT", url, json_data=body,
                             min_interval=interval, allow_status=allow)
    if response.status_code != 400:
        return True, False
    if "profile picture" not in response.text.lower():
        raise RuntimeError(f"update rejected [400]: {response.text[:200]}")
    remaining = {k: v for k, v in body.items() if k != "profilePictureUrl"}
    if not remaining:
        return False, True
    paced_request(client, "PUT", url, json_data=remaining, min_interval=interval)
    return True, True


def _set_lock(client, user_id, action, interval):
    """POST .../users/v1/{id}/lock or /unlock (verified 200, idempotent)."""
    url = f"{client.base_url}{USER_PATH}/{user_id}/{action}"
    paced_request(client, "POST", url, json_data={}, min_interval=interval)


def migrate_user_attributes(source_client, destination_client):
    log_section("User Attributes")

    unlock_enabled = (os.getenv("USER_ATTRIBUTES_UNLOCK", "false")
                      .strip().lower() == "true")
    interval = float(os.getenv("USER_ATTRIBUTES_MIN_INTERVAL_SECONDS",
                               INTERVAL_GENERAL))

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

    matched = updated = locked = unlocked = in_sync = failed = 0
    pictures_restricted = []   # emails whose picture URL the destination rejected
    left_locked = []           # locked only in destination; unlock disabled
    missing = []               # source emails with no destination user
    drift = []                 # (email, [read-only fields that differ])

    for user in source_users:
        email = (user.get("email") or "").strip().lower()
        if not email:
            continue
        dest_user = dest_by_email.get(email)
        if not dest_user:
            missing.append(email)
            continue
        matched += 1

        dest_id = dest_user.get("id") or dest_user.get("_id")
        if not dest_id:
            failed += 1
            log_error(f"  {email}: destination user has no id field; skipped")
            continue

        body = {}
        for field in WRITABLE_FIELDS:
            value = user.get(field)
            if (_is_meaningful(field, value)
                    and _values_differ(field, value, dest_user.get(field))):
                body[field] = value

        drift_fields = _read_only_drift(user, dest_user)
        if drift_fields:
            drift.append((email, drift_fields))

        source_locked = bool(user.get("isLocked"))
        dest_locked = bool(dest_user.get("isLocked"))

        try:
            did_update = picture_restricted = lock_changed = left_locked_here = False
            if body:
                did_update, picture_restricted = _update_user(
                    destination_client, dest_id, body, interval)
            if did_update:
                updated += 1
                sent = [f for f in body if not (picture_restricted
                                                and f == "profilePictureUrl")]
                log_success(f"  {email}: updated {', '.join(sorted(sent))}")
            if picture_restricted:
                pictures_restricted.append(email)

            if source_locked and not dest_locked:
                _set_lock(destination_client, dest_id, "lock", interval)
                locked += 1
                lock_changed = True
                log_success(f"  {email}: locked (locked in source)")
            elif dest_locked and not source_locked:
                if unlock_enabled:
                    _set_lock(destination_client, dest_id, "unlock", interval)
                    unlocked += 1
                    lock_changed = True
                    log_success(f"  {email}: unlocked (USER_ATTRIBUTES_UNLOCK=true)")
                else:
                    left_locked.append(email)
                    left_locked_here = True

            if not (did_update or picture_restricted or lock_changed
                    or left_locked_here):
                in_sync += 1
        except Exception as e:
            failed += 1
            log_error(f"  {email}: {e}")

    if missing:
        log_warning(f"{len(missing)} source users do not exist in the destination "
                    "and were skipped (run user migration first):")
        for email in missing[:10]:
            logger.info(f"    {email}")
    if pictures_restricted:
        log_warning(f"{len(pictures_restricted)} profile pictures were rejected by the "
                    "destination's allowed-domains list (allow the domain in the "
                    "portal, then re-run this step):")
        for email in pictures_restricted[:10]:
            logger.info(f"    {email}")
    if left_locked:
        log_warning(f"{len(left_locked)} users are locked only in the destination — "
                    "left locked (set USER_ATTRIBUTES_UNLOCK=true to unlock them):")
        for email in left_locked[:10]:
            logger.info(f"    {email}")
    if drift:
        log_warning(f"{len(drift)} users differ on attributes the public API cannot "
                    "write (mfaEnrolled/verified/provider/managedBy — Frontegg "
                    "Support migrates these, along with passwords and MFA secrets):")
        for email, fields in drift[:10]:
            logger.info(f"    {email} — {', '.join(fields)}")

    logger.print_stats("User Attribute Summary", {
        "Matched users": matched,
        "Updated": updated,
        "Locked": locked,
        "Unlocked": unlocked,
        "Pictures restricted": len(pictures_restricted),
        "In sync": in_sync,
        "Missing in destination": len(missing),
        "Read-only drift": len(drift),
        "Failed": failed,
    })
    if failed:
        log_error(f"User attribute migration finished with {failed} failure(s).")
    else:
        log_success("User attribute migration completed.")
