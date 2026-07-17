import os
import json
import requests
from dotenv import load_dotenv
from utility.logger import get_logger, log_subsection, log_success, log_warning, log_error

load_dotenv()

MIGRATE_ENTITLEMENTS = os.getenv("MIGRATE_ENTITLEMENTS", "False").lower() == "true"

FEATURES_ENDPOINT = "/resources/features/v1"
PLANS_ENDPOINT = "/resources/plans/v1"
PLAN_FEATURES_ENDPOINT = "/resources/plans/v1/{plan_id}/features"
PLAN_LINK_FEATURES_ENDPOINT = "/resources/plans/v1/{plan_id}/features/link"
FEATURE_FLAGS_ENDPOINT = "/resources/feature-flags/v1"
ENTITLEMENTS_ENDPOINT = "/resources/entitlements/v2"
ENTITLEMENTS_BATCH_ENDPOINT = "/resources/entitlements/v2/batch"
IDENTITY_USERS_ENDPOINT = "/identity/resources/users/v3"


def _headers(client):
    return {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def _entitlements_url(client, endpoint):
    return f"{client.base_url}/entitlements{endpoint}"


def _http_error_details(error):
    if error.response is None:
        return str(error)

    status = error.response.status_code
    try:
        payload = error.response.json()
        payload_text = json.dumps(payload, ensure_ascii=True)
    except Exception:
        payload_text = error.response.text
    return f"status={status}, payload={payload_text}"


def _extract_items(response_json):
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        if isinstance(response_json.get("items"), list):
            return response_json["items"]
    return []


def _extract_has_next(response_json):
    return bool(response_json.get("hasNext")) if isinstance(response_json, dict) else False


def _paginated_get(client, endpoint, limit=100):
    items = []
    offset = 0
    headers = _headers(client)

    while True:
        url = _entitlements_url(client, endpoint)
        response = client.session.get(url, headers=headers, params={"offset": offset, "limit": limit})
        response.raise_for_status()

        data = response.json()
        page_items = _extract_items(data)
        items.extend(page_items)

        has_next = _extract_has_next(data)
        if not has_next or not page_items:
            break
        offset += limit

    return items


def get_features(client):
    return _paginated_get(client, FEATURES_ENDPOINT, limit=100)


def create_feature(client, payload):
    url = _entitlements_url(client, FEATURES_ENDPOINT)
    response = client.session.post(url, headers=_headers(client), json=payload)
    response.raise_for_status()
    return response.json() if response.content else {}


def get_plans(client):
    return _paginated_get(client, PLANS_ENDPOINT, limit=100)


def get_plan_features(client, plan_id):
    endpoint = PLAN_FEATURES_ENDPOINT.format(plan_id=plan_id)
    return _paginated_get(client, endpoint, limit=100)


def create_plan(client, payload):
    url = _entitlements_url(client, PLANS_ENDPOINT)
    response = client.session.post(url, headers=_headers(client), json=payload)
    response.raise_for_status()
    return response.json() if response.content else {}


def link_features_to_plan(client, plan_id, feature_ids):
    if not feature_ids:
        return
    url = _entitlements_url(client, PLAN_LINK_FEATURES_ENDPOINT.format(plan_id=plan_id))
    response = client.session.patch(
        url,
        headers=_headers(client),
        json={"featuresIds": feature_ids},
    )
    response.raise_for_status()


def get_feature_flags(client):
    # API constraint: this endpoint rejects limits > 10.
    return _paginated_get(client, FEATURE_FLAGS_ENDPOINT, limit=10)


def create_feature_flag(client, payload):
    url = _entitlements_url(client, FEATURE_FLAGS_ENDPOINT)
    response = client.session.post(url, headers=_headers(client), json=payload)
    response.raise_for_status()
    return response.json() if response.content else {}


def get_entitlements(client):
    # API constraint: this endpoint rejects limits > 10.
    return _paginated_get(client, ENTITLEMENTS_ENDPOINT, limit=10)


def batch_create_entitlements(client, entitlements, batch_size=200):
    if not entitlements:
        return 0

    url = _entitlements_url(client, ENTITLEMENTS_BATCH_ENDPOINT)
    headers = _headers(client)
    created = 0
    batch_size = max(int(batch_size or 1), 1)

    for start in range(0, len(entitlements), batch_size):
        batch = entitlements[start:start + batch_size]
        response = client.session.post(url, headers=headers, json={"entitlements": batch})
        response.raise_for_status()
        created += len(batch)

    return created


def get_users_by_id_and_email(client, page_limit=200):
    users_by_id = {}
    user_id_by_email = {}
    page_index = 0
    total_pages = None

    while True:
        url = f"{client.base_url}{IDENTITY_USERS_ENDPOINT}"
        params = {
            "includeSubTenants": "true",
            "_limit": page_limit,
            "_offset": page_index,
        }
        response = client.session.get(url, headers=_headers(client), params=params)
        response.raise_for_status()
        data = response.json()

        items = data.get("items", []) if isinstance(data, dict) else []
        metadata = data.get("_metadata", {}) if isinstance(data, dict) else {}
        page_total = metadata.get("totalPages")
        if isinstance(page_total, int) and page_total > 0:
            total_pages = page_total

        if not items:
            break

        for user in items:
            if not isinstance(user, dict):
                continue
            user_id = user.get("id")
            email = user.get("email")
            if user_id:
                users_by_id[user_id] = user
            if user_id and email:
                user_id_by_email[email.strip().lower()] = user_id

        if total_pages is not None and page_index >= total_pages - 1:
            break
        page_index += 1

    return users_by_id, user_id_by_email


def _build_feature_payload(source_feature):
    payload = {
        "name": source_feature.get("name"),
        "key": source_feature.get("key"),
        "description": source_feature.get("description") or "",
    }
    permissions = source_feature.get("permissions")
    if isinstance(permissions, list):
        payload["permissions"] = permissions
    else:
        payload["permissions"] = []
    return payload


def _build_plan_payload(source_plan):
    payload = {
        "name": source_plan.get("name"),
        "defaultTreatment": source_plan.get("defaultTreatment"),
        "description": source_plan.get("description") or "",
        "assignOnSignup": bool(source_plan.get("assignOnSignup", False)),
    }

    rules = source_plan.get("rules")
    if isinstance(rules, list):
        payload["rules"] = rules

    metadata = source_plan.get("metadata")
    if metadata is not None:
        # API expects metadata as a JSON string.
        if isinstance(metadata, str):
            payload["metadata"] = metadata
        else:
            payload["metadata"] = json.dumps(metadata)

    if source_plan.get("defaultTimeLimitation") is not None:
        payload["defaultTimeLimitation"] = source_plan.get("defaultTimeLimitation")

    return payload


def _extract_feature_flag_feature_id(flag):
    if not isinstance(flag, dict):
        return None
    if flag.get("featureId"):
        return flag.get("featureId")
    feature = flag.get("feature")
    if isinstance(feature, dict):
        return feature.get("id")
    return None


def _build_feature_flag_payload(source_flag, destination_feature_id):
    payload = {
        "name": source_flag.get("name"),
        "on": bool(source_flag.get("on", False)),
        "offTreatment": source_flag.get("offTreatment"),
        "defaultTreatment": source_flag.get("defaultTreatment"),
        "featureId": destination_feature_id,
    }

    if source_flag.get("description") is not None:
        payload["description"] = source_flag.get("description")
    if source_flag.get("metadata") is not None:
        payload["metadata"] = source_flag.get("metadata")

    rules = source_flag.get("rules")
    if isinstance(rules, list):
        payload["rules"] = rules

    return payload


def _extract_plan_feature_key(plan_feature):
    if not isinstance(plan_feature, dict):
        return None

    feature_key = plan_feature.get("key") or plan_feature.get("featureKey")
    if feature_key:
        return feature_key

    nested_feature = plan_feature.get("feature")
    if isinstance(nested_feature, dict):
        return nested_feature.get("key")

    return None


def _extract_plan_feature_id(plan_feature):
    if not isinstance(plan_feature, dict):
        return None

    feature_id = plan_feature.get("id") or plan_feature.get("featureId")
    if feature_id:
        return feature_id

    nested_feature = plan_feature.get("feature")
    if isinstance(nested_feature, dict):
        return nested_feature.get("id")

    return None


def migrate_entitlements(source_client, destination_client):
    logger = get_logger()

    if not MIGRATE_ENTITLEMENTS:
        log_warning("Entitlements migration is disabled (MIGRATE_ENTITLEMENTS=False)")
        return

    logger.section("Entitlements Migration")
    log_subsection("Migrating Features")

    try:
        source_features = get_features(source_client)
        destination_features = get_features(destination_client)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status in (403, 404):
            log_warning(f"Entitlements features endpoint unavailable ({status}); skipping entitlements migration.")
            return
        log_error(f"Failed fetching features: {_http_error_details(e)}")
        return
    except Exception as e:
        log_error(f"Failed fetching features: {e}")
        return

    source_feature_by_id = {f.get("id"): f for f in source_features if isinstance(f, dict) and f.get("id")}
    destination_feature_id_by_key = {
        f.get("key"): f.get("id")
        for f in destination_features
        if isinstance(f, dict) and f.get("key") and f.get("id")
    }

    features_created = 0
    features_skipped = 0
    features_failed = 0

    for source_feature in source_features:
        source_key = source_feature.get("key")
        if not source_key:
            features_skipped += 1
            continue

        if source_key in destination_feature_id_by_key:
            features_skipped += 1
            continue

        payload = _build_feature_payload(source_feature)
        try:
            created_feature = create_feature(destination_client, payload)
            created_id = created_feature.get("id")
            if created_id:
                destination_feature_id_by_key[source_key] = created_id
            features_created += 1
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            response_text = (e.response.text or "").lower() if e.response is not None else ""
            if status in (400, 409) and ("already" in response_text or "exists" in response_text):
                features_skipped += 1
                continue
            features_failed += 1
            log_error(f"Failed creating feature '{source_key}': {_http_error_details(e)}")
        except Exception as e:
            features_failed += 1
            log_error(f"Unexpected error creating feature '{source_key}': {e}")

    log_subsection("Migrating Feature Flags")
    feature_flags_created = 0
    feature_flags_skipped_existing = 0
    feature_flags_skipped_unmapped_feature = 0
    feature_flags_failed = 0

    try:
        source_feature_flags = get_feature_flags(source_client)
        destination_feature_flags = get_feature_flags(destination_client)
    except requests.exceptions.HTTPError as e:
        log_error(f"Failed fetching feature flags: {_http_error_details(e)}")
        source_feature_flags = []
        destination_feature_flags = []
    except Exception as e:
        log_error(f"Failed fetching feature flags: {e}")
        source_feature_flags = []
        destination_feature_flags = []

    source_feature_id_to_key = {
        feature_id: feature.get("key")
        for feature_id, feature in source_feature_by_id.items()
        if isinstance(feature, dict) and feature.get("key")
    }

    destination_feature_flags_by_name = {
        flag.get("name"): flag
        for flag in destination_feature_flags
        if isinstance(flag, dict) and flag.get("name")
    }

    for source_flag in source_feature_flags:
        if not isinstance(source_flag, dict):
            continue

        source_flag_name = source_flag.get("name")
        if not source_flag_name:
            feature_flags_failed += 1
            continue

        if source_flag_name in destination_feature_flags_by_name:
            feature_flags_skipped_existing += 1
            continue

        source_feature_id = _extract_feature_flag_feature_id(source_flag)
        source_feature_key = source_feature_id_to_key.get(source_feature_id)
        destination_feature_id = destination_feature_id_by_key.get(source_feature_key)
        if not destination_feature_id:
            feature_flags_skipped_unmapped_feature += 1
            continue

        payload = _build_feature_flag_payload(source_flag, destination_feature_id)
        try:
            created_flag = create_feature_flag(destination_client, payload)
            created_name = created_flag.get("name")
            if created_name:
                destination_feature_flags_by_name[created_name] = created_flag
            feature_flags_created += 1
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            response_text = (e.response.text or "").lower() if e.response is not None else ""
            if status in (400, 409) and ("already" in response_text or "exists" in response_text):
                feature_flags_skipped_existing += 1
                continue
            feature_flags_failed += 1
            log_error(f"Failed creating feature flag '{source_flag_name}': {_http_error_details(e)}")
        except Exception as e:
            feature_flags_failed += 1
            log_error(f"Unexpected error creating feature flag '{source_flag_name}': {e}")

    log_subsection("Migrating Plans")

    try:
        source_plans = get_plans(source_client)
        destination_plans = get_plans(destination_client)
    except requests.exceptions.HTTPError as e:
        log_error(f"Failed fetching plans: {_http_error_details(e)}")
        return
    except Exception as e:
        log_error(f"Failed fetching plans: {e}")
        return

    destination_plan_id_by_name = {
        plan.get("name"): plan.get("id")
        for plan in destination_plans
        if isinstance(plan, dict) and plan.get("name") and plan.get("id")
    }
    source_plan_id_to_name = {
        plan.get("id"): plan.get("name")
        for plan in source_plans
        if isinstance(plan, dict) and plan.get("id") and plan.get("name")
    }

    plans_created = 0
    plans_skipped = 0
    plans_failed = 0
    links_applied = 0
    links_skipped = 0
    links_failed = 0

    for source_plan in source_plans:
        plan_name = source_plan.get("name")
        if not plan_name:
            plans_skipped += 1
            continue

        destination_plan_id = destination_plan_id_by_name.get(plan_name)
        if not destination_plan_id:
            payload = _build_plan_payload(source_plan)
            try:
                created_plan = create_plan(destination_client, payload)
                destination_plan_id = created_plan.get("id")
                if not destination_plan_id:
                    plans_failed += 1
                    log_error(f"Created plan '{plan_name}' but no destination plan ID returned.")
                    continue
                destination_plan_id_by_name[plan_name] = destination_plan_id
                plans_created += 1
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                response_text = (e.response.text or "").lower() if e.response is not None else ""
                if status in (400, 409) and ("already" in response_text or "exists" in response_text):
                    plans_skipped += 1
                    continue
                plans_failed += 1
                log_error(f"Failed creating plan '{plan_name}': {_http_error_details(e)}")
                continue
            except Exception as e:
                plans_failed += 1
                log_error(f"Unexpected error creating plan '{plan_name}': {e}")
                continue
        else:
            plans_skipped += 1

        source_plan_id = source_plan.get("id")
        if not source_plan_id:
            links_skipped += 1
            continue

        try:
            source_plan_features = get_plan_features(source_client, source_plan_id)
            destination_plan_features = get_plan_features(destination_client, destination_plan_id)
        except Exception as e:
            links_failed += 1
            log_error(f"Failed fetching plan features for '{plan_name}': {e}")
            continue

        source_feature_keys = set()
        for plan_feature in source_plan_features:
            source_key = _extract_plan_feature_key(plan_feature)
            if not source_key:
                source_feature_id = _extract_plan_feature_id(plan_feature)
                source_feature = source_feature_by_id.get(source_feature_id)
                source_key = source_feature.get("key") if isinstance(source_feature, dict) else None
            if source_key:
                source_feature_keys.add(source_key)

        if not source_feature_keys:
            links_skipped += 1
            continue

        destination_feature_ids_to_link = {
            destination_feature_id_by_key[key]
            for key in source_feature_keys
            if key in destination_feature_id_by_key
        }

        if not destination_feature_ids_to_link:
            links_skipped += 1
            continue

        destination_existing_feature_ids = {
            _extract_plan_feature_id(plan_feature)
            for plan_feature in destination_plan_features
            if _extract_plan_feature_id(plan_feature)
        }
        missing_feature_ids = sorted(destination_feature_ids_to_link - destination_existing_feature_ids)

        if not missing_feature_ids:
            links_skipped += 1
            continue

        try:
            link_features_to_plan(destination_client, destination_plan_id, missing_feature_ids)
            links_applied += len(missing_feature_ids)
        except Exception as e:
            links_failed += 1
            log_error(f"Failed linking features to plan '{plan_name}': {e}")

    log_subsection("Migrating Entitlements Assignments")
    entitlements_created = 0
    entitlements_skipped_existing = 0
    entitlements_skipped_unmapped_plan = 0
    entitlements_skipped_unmapped_user = 0
    entitlements_failed = 0

    try:
        source_entitlements = get_entitlements(source_client)
        destination_entitlements = get_entitlements(destination_client)
        source_users_by_id, _ = get_users_by_id_and_email(source_client)
        _, destination_user_id_by_email = get_users_by_id_and_email(destination_client)
    except requests.exceptions.HTTPError as e:
        entitlements_failed += 1
        log_error(f"Failed fetching entitlements/users for assignment migration: {_http_error_details(e)}")
        source_entitlements = []
        destination_entitlements = []
        source_users_by_id = {}
        destination_user_id_by_email = {}
    except Exception as e:
        entitlements_failed += 1
        log_error(f"Unexpected error fetching entitlements/users for assignment migration: {e}")
        source_entitlements = []
        destination_entitlements = []
        source_users_by_id = {}
        destination_user_id_by_email = {}

    destination_plan_id_by_source_plan_id = {}
    for source_plan_id, source_plan_name in source_plan_id_to_name.items():
        destination_plan_id = destination_plan_id_by_name.get(source_plan_name)
        if destination_plan_id:
            destination_plan_id_by_source_plan_id[source_plan_id] = destination_plan_id

    destination_entitlement_signatures = {
        (
            ent.get("planId"),
            ent.get("tenantId"),
            ent.get("userId"),
            ent.get("expirationDate"),
        )
        for ent in destination_entitlements
        if isinstance(ent, dict) and ent.get("planId") and ent.get("tenantId")
    }

    entitlements_to_create = []
    pending_signatures = set()
    for source_entitlement in source_entitlements:
        if not isinstance(source_entitlement, dict):
            continue

        source_plan_id = source_entitlement.get("planId")
        destination_plan_id = destination_plan_id_by_source_plan_id.get(source_plan_id)
        if not destination_plan_id:
            entitlements_skipped_unmapped_plan += 1
            continue

        tenant_id = source_entitlement.get("tenantId")
        if not tenant_id:
            entitlements_skipped_unmapped_plan += 1
            continue

        destination_user_id = None
        source_user_id = source_entitlement.get("userId")
        if source_user_id:
            source_user = source_users_by_id.get(source_user_id)
            source_email = source_user.get("email").strip().lower() if isinstance(source_user, dict) and source_user.get("email") else None
            if not source_email:
                entitlements_skipped_unmapped_user += 1
                continue
            destination_user_id = destination_user_id_by_email.get(source_email)
            if not destination_user_id:
                entitlements_skipped_unmapped_user += 1
                continue

        expiration_date = source_entitlement.get("expirationDate")
        signature = (destination_plan_id, tenant_id, destination_user_id, expiration_date)
        if signature in destination_entitlement_signatures or signature in pending_signatures:
            entitlements_skipped_existing += 1
            continue

        payload = {
            "planId": destination_plan_id,
            "tenantId": tenant_id,
        }
        if destination_user_id:
            payload["userId"] = destination_user_id
        if expiration_date:
            payload["expirationDate"] = expiration_date

        entitlements_to_create.append(payload)
        pending_signatures.add(signature)

    if entitlements_to_create:
        try:
            entitlements_created = batch_create_entitlements(destination_client, entitlements_to_create, batch_size=200)
        except requests.exceptions.HTTPError as e:
            entitlements_failed += len(entitlements_to_create)
            log_error(f"Failed creating entitlements in batch: {_http_error_details(e)}")
        except Exception as e:
            entitlements_failed += len(entitlements_to_create)
            log_error(f"Unexpected error creating entitlements in batch: {e}")

    logger.print_stats("Entitlements Migration Summary", {
        "Source Features": len(source_features),
        "Features Created": features_created,
        "Features Skipped": features_skipped,
        "Features Failed": features_failed,
        "Source Feature Flags": len(source_feature_flags),
        "Feature Flags Created": feature_flags_created,
        "Feature Flags Skipped (Existing)": feature_flags_skipped_existing,
        "Feature Flags Skipped (Unmapped Feature)": feature_flags_skipped_unmapped_feature,
        "Feature Flags Failed": feature_flags_failed,
        "Source Plans": len(source_plans),
        "Plans Created": plans_created,
        "Plans Skipped": plans_skipped,
        "Plans Failed": plans_failed,
        "Feature Links Applied": links_applied,
        "Feature Links Skipped": links_skipped,
        "Feature Links Failed": links_failed,
        "Source Entitlements": len(source_entitlements),
        "Entitlements Created": entitlements_created,
        "Entitlements Skipped (Existing)": entitlements_skipped_existing,
        "Entitlements Skipped (Unmapped Plan)": entitlements_skipped_unmapped_plan,
        "Entitlements Skipped (Unmapped User)": entitlements_skipped_unmapped_user,
        "Entitlements Failed": entitlements_failed,
    })
    log_success("Entitlements migration completed")
