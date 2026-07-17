import os
import json
import requests
from dotenv import load_dotenv
from utility.logger import get_logger, log_subsection, log_success, log_warning, log_error

load_dotenv()

MIGRATE_API_ACCESS_CONTROL = os.getenv("MIGRATE_API_ACCESS_CONTROL", "False").lower() == "true"
ROUTES_ENDPOINT = "/resources/routes/v1"


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


def get_routes(client):
    url = _entitlements_url(client, ROUTES_ENDPOINT)
    response = client.session.get(url, headers=_headers(client))
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def create_route(client, payload):
    url = _entitlements_url(client, ROUTES_ENDPOINT)
    response = client.session.post(url, headers=_headers(client), json=payload)
    response.raise_for_status()
    return response.json() if response.content else {}


def _route_signature(route):
    return (
        route.get("method"),
        route.get("path"),
    )


def _build_create_payload(source_route):
    payload = {
        "method": source_route.get("method"),
        "path": source_route.get("path"),
        "policyType": source_route.get("policyType"),
        "description": source_route.get("description") or "",
    }
    if not payload["method"] or not payload["path"] or not payload["policyType"]:
        return None
    return payload


def migrate_api_access_control(source_client, destination_client):
    logger = get_logger()

    if not MIGRATE_API_ACCESS_CONTROL:
        log_warning("API access control migration is disabled (MIGRATE_API_ACCESS_CONTROL=False)")
        return

    logger.section("API Access Control Migration")
    log_subsection("Migrating Routes")

    try:
        source_routes = get_routes(source_client)
        destination_routes = get_routes(destination_client)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status in (403, 404):
            log_warning(f"API access control routes endpoint unavailable ({status}); skipping.")
            return
        log_error(f"Failed fetching routes: {_http_error_details(e)}")
        return
    except Exception as e:
        log_error(f"Failed fetching routes: {e}")
        return

    destination_signatures = {
        _route_signature(route)
        for route in destination_routes
        if isinstance(route, dict) and route.get("method") and route.get("path")
    }

    created = 0
    skipped_existing = 0
    skipped_invalid = 0
    failed = 0

    for source_route in source_routes:
        if not isinstance(source_route, dict):
            skipped_invalid += 1
            continue

        signature = _route_signature(source_route)
        if signature in destination_signatures:
            skipped_existing += 1
            continue

        payload = _build_create_payload(source_route)
        if not payload:
            skipped_invalid += 1
            continue

        try:
            created_route = create_route(destination_client, payload)
            created_signature = _route_signature(created_route) if isinstance(created_route, dict) else signature
            destination_signatures.add(created_signature)
            created += 1
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            response_text = (e.response.text or "").lower() if e.response is not None else ""
            if status in (400, 409) and ("already" in response_text or "exists" in response_text):
                skipped_existing += 1
                continue
            if status == 400 and "found conflicted parameter on the same hierarchical level" in response_text:
                skipped_existing += 1
                continue
            failed += 1
            log_error(
                f"Failed creating route {payload.get('method')} {payload.get('path')}: "
                f"{_http_error_details(e)}"
            )
        except Exception as e:
            failed += 1
            log_error(f"Unexpected error creating route {payload.get('method')} {payload.get('path')}: {e}")

    logger.print_stats("API Access Control Migration Summary", {
        "Source Routes": len(source_routes),
        "Destination Routes": len(destination_routes),
        "Created": created,
        "Skipped (Existing)": skipped_existing,
        "Skipped (Invalid)": skipped_invalid,
        "Failed": failed,
    })
    log_success("API access control migration completed")
