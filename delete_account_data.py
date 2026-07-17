import json
import requests
import os
import time
import random
import re
import logging
import sys
from dotenv import load_dotenv

# Dedicated deletion log (separate from migration.log) at the project root.
_DELETE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delete.log")
_delete_logger = logging.getLogger("delete_account_data")
_delete_logger.setLevel(logging.INFO)
_delete_logger.propagate = False
if not _delete_logger.handlers:
    _delete_file_handler = logging.FileHandler(_DELETE_LOG_PATH, encoding="utf-8")
    _delete_file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    _delete_logger.addHandler(_delete_file_handler)


def _log(*args):
    """Print to console and append the same line to delete.log."""
    message = " ".join(str(a) for a in args)
    sys.stdout.write(message + "\n")
    _delete_logger.info(message)


# Constants - Source account
load_dotenv()
BASE_URL = os.getenv("BASE_URL_2")
CLIENT_ID = os.getenv("CLIENT_ID_2")
API_KEY = os.getenv("API_KEY_2")

# Deletion Flags - Read from environment variables
DELETE_TENANTS = os.getenv("DELETE_TENANTS", "False").lower() == "true"
DELETE_USERS = os.getenv("DELETE_USERS", "False").lower() == "true"
DELETE_PERMISSIONS = os.getenv("DELETE_PERMISSIONS", "False").lower() == "true"
DELETE_ROLES = os.getenv("DELETE_ROLES", "False").lower() == "true"
DELETE_APPLICATIONS = os.getenv("DELETE_APPLICATIONS", "False").lower() == "true"
DELETE_PREHOOKS = os.getenv("DELETE_PREHOOKS", "False").lower() == "true"

# Rate limit configuration (same pattern as migration modules)
DEFAULT_RATE_LIMIT = 30  # requests/minute per endpoint
RATE_LIMITS = {
    # Override per-endpoint if needed, e.g.:
    # Applications deletes are usually the hottest path:
    f"{BASE_URL}/applications/resources/applications/v1": 10,
}
_last_request_times = {}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

def _rate_limit_key(url: str) -> str:
    """
    Normalize URLs so per-resource URLs share the same bucket.
    Example:
      /applications/.../v1/<appId> -> /applications/.../v1
    """
    base = url.split("?", 1)[0]
    parts = base.rstrip("/").split("/")
    if len(parts) >= 2 and _UUID_RE.match(parts[-1]):
        # Collapse per-resource endpoints into their collection endpoint.
        # This is important for rate limiting deletes: each app has a different URL.
        return "/".join(parts[:-1])
    return base.rstrip("/")

def _get_rate_limit(endpoint: str) -> int:
    return RATE_LIMITS.get(endpoint, DEFAULT_RATE_LIMIT)

def enforce_rate_limit(endpoint: str):
    key = _rate_limit_key(endpoint)
    interval = 60 / _get_rate_limit(key)
    last = _last_request_times.get(key)
    if last is not None:
        elapsed = time.time() - last
        if elapsed < interval:
            time.sleep(interval - elapsed)
    _last_request_times[key] = time.time()

def make_request_with_rate_limiting(method, url, headers=None, params=None, json_data=None):
    """
    Rate-limited request with simple 429 retry.
    Mirrors behavior used in migration modules.
    """
    enforce_rate_limit(url)
    response = requests.request(method, url, headers=headers, params=params, json=json_data)
    if response.status_code == 429:
        _log("⚠ Rate limit exceeded. Retrying after delay...")
        time.sleep(60)
        enforce_rate_limit(url)
        response = requests.request(method, url, headers=headers, params=params, json=json_data)
    response.raise_for_status()
    return response

def get_vendor_token():
    """Fetches the vendor token using CLIENT_ID and API_KEY."""
    url = f"{BASE_URL}/auth/vendor/"
    headers = {
        'accept': 'application/json',
        'content-type': 'application/json'
    }
    data = {
        "clientId": CLIENT_ID,
        "secret": API_KEY
    }
    response = make_request_with_rate_limiting("POST", url, headers=headers, json_data=data)
    return response.json().get("token")

def get_all_users(token):
    """Fetches all users using the vendor token."""
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    # Some environments don't support /users/v2 (404). Prefer /users/v3 and fall back.
    candidate_urls = [
        f"{BASE_URL}/identity/resources/users/v3",
        f"{BASE_URL}/identity/resources/users/v2",
    ]

    users = []

    page_limit = 200

    for base in candidate_urls:
        try:
            page_index = 0
            total_pages = None
            while True:
                params = {
                    "_limit": page_limit,
                    "_offset": page_index,
                    "includeSubTenants": "true",
                }
                response = make_request_with_rate_limiting("GET", base, headers=headers, params=params)
                data = response.json()
                items = data.get("items", []) if isinstance(data, dict) else []
                metadata = data.get("_metadata", {}) if isinstance(data, dict) else {}
                page_total = metadata.get("totalPages")
                if isinstance(page_total, int) and page_total > 0:
                    total_pages = page_total
                users.extend(items)

                if total_pages is not None and page_index >= total_pages - 1:
                    break
                if not items:
                    break
                page_index += 1

            _log(f"Retrieved {len(users)} users.")
            return users
        except requests.exceptions.HTTPError as e:
            # Try next candidate on 404 only; re-raise other HTTP errors.
            if e.response is not None and e.response.status_code == 404:
                users = []
                continue
            raise

    raise RuntimeError(
        "Could not list users: both /identity/resources/users/v3 and /identity/resources/users/v2 returned 404."
    )

def delete_user(token, user_id):
    """Deletes a user by ID using the vendor token."""
    url = f"{BASE_URL}/identity/resources/users/v1/{user_id}"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        make_request_with_rate_limiting("DELETE", url, headers=headers)
        _log(f"Deleted user with ID: {user_id}")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            _log(f"User with ID {user_id} not found (404), skipping...")
        else:
            _log(f"Failed to delete user with ID {user_id}: {e}")

def get_tenant_ids(token):
    """Fetches tenant IDs using the vendor token."""
    url = f"{BASE_URL}/tenants/resources/tenants/v2"
    headers = {'Authorization': f'Bearer {token}'}
    tenant_ids = []
    page_limit = 200
    page_index = 0
    total_pages = None

    while True:
        params = {
            "_limit": page_limit,
            "_offset": page_index,
        }
        response = make_request_with_rate_limiting("GET", url, headers=headers, params=params)
        payload = response.json() if isinstance(response.json(), dict) else {}
        items = payload.get("items", [])
        metadata = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
        page_total = metadata.get("totalPages")
        if isinstance(page_total, int) and page_total > 0:
            total_pages = page_total
        tenant_ids.extend([tenant["tenantId"] for tenant in payload.get("items", []) if tenant.get("tenantId")])
        if total_pages is not None and page_index >= total_pages - 1:
            break
        if not items:
            break
        page_index += 1

    return tenant_ids

def delete_tenant(token, tenant_id):
    """Deletes a tenant by ID."""
    url = f"{BASE_URL}/tenants/resources/tenants/v1/{tenant_id}"
    headers = {'Authorization': f'Bearer {token}'}
    make_request_with_rate_limiting("DELETE", url, headers=headers)
    _log(f"Deleted tenant with ID: {tenant_id}")

def get_permissions(token):
    """Fetches all permissions using the vendor token."""
    url = f"{BASE_URL}/identity/resources/permissions/v1"
    headers = {'Authorization': f'Bearer {token}'}
    response = make_request_with_rate_limiting("GET", url, headers=headers)
    return [permission["id"] for permission in response.json()]

def delete_permission(token, permission_id):
    """Deletes a permission by ID.

    Returns one of: 'deleted', 'missing' (404 — already removed, e.g. by a
    cascade when a related permission was deleted), or 'failed'.
    """
    url = f"{BASE_URL}/identity/resources/permissions/v1/{permission_id}"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        make_request_with_rate_limiting("DELETE", url, headers=headers)
        return "deleted"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            return "missing"
        # System / FE-managed permissions may be protected (400/403). Don't
        # abort the whole cleanup; report and continue.
        _log(f"✗ Failed to delete permission {permission_id}: {e}")
        return "failed"

def get_roles(token):
    """Fetches all roles using the vendor token."""
    url = f"{BASE_URL}/identity/resources/roles/v2?_limit=2000"
    headers = {'Authorization': f'Bearer {token}'}
    response = make_request_with_rate_limiting("GET", url, headers=headers)
    return [role["id"] for role in response.json().get("items", [])]

def delete_role(token, role_id):
    """Deletes a role by ID using the vendor token."""
    url = f"{BASE_URL}/identity/resources/roles/v1/{role_id}"
    headers = {'Authorization': f'Bearer {token}'}
    try:
        make_request_with_rate_limiting("DELETE", url, headers=headers)
        _log(f"Deleted role with ID: {role_id}")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            _log(f"Role with ID {role_id} not found (404), skipping...")
        else:
            _log(f"Failed to delete role with ID {role_id}: {e}")

def get_applications(token):
    """Fetches all applications using the vendor token."""
    url = f"{BASE_URL}/applications/resources/applications/v1?_excludeAgents=true"
    headers = {'Authorization': f'Bearer {token}'}
    response = make_request_with_rate_limiting("GET", url, headers=headers)
    applications = response.json()
    _log(f"Retrieved {len(applications)} applications.")
    return applications

def create_dummy_application(token):
    """Creates a temporary dummy application to allow deletion of all other apps."""
    url = f"{BASE_URL}/applications/resources/applications/v1"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    data = {
        "name": "Temporary Dummy App",
        "appURL": "https://dummy.example.com",
        "loginURL": "https://dummy.example.com/login",
        "accessType": "FREE_ACCESS",
        "isActive": True,
        "type": "web",
        "frontendStack": "react",
        "description": "Temporary app for deletion process"
    }
    try:
        response = make_request_with_rate_limiting("POST", url, headers=headers, json_data=data)
        app_data = response.json()
        _log(f"✓ Created dummy application: {app_data.get('name')} (ID: {app_data.get('id')})")
        return app_data
    except requests.exceptions.HTTPError as e:
        _log(f"✗ Failed to create dummy application: {e}")
        return None

def delete_application(token, app_id, app_name=None):
    """Deletes an application by ID using the vendor token."""
    url = f"{BASE_URL}/applications/resources/applications/v1/{app_id}"
    headers = {'Authorization': f'Bearer {token}'}
    display_name = app_name if app_name else app_id
    max_attempts = 6
    base_delay_s = 1.0
    max_delay_s = 60.0

    for attempt in range(1, max_attempts + 1):
        try:
            enforce_rate_limit(url)
            response = requests.delete(url, headers=headers)

            # Retry on rate limit / transient server errors
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == max_attempts:
                    _log(f"✗ Failed to delete application {display_name}: {response.status_code} - {response.text}")
                    return False

                retry_after = response.headers.get("Retry-After")
                try:
                    retry_after_s = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_s = None

                backoff_s = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
                jitter_s = random.uniform(0, 0.25 * backoff_s)
                # For 429 specifically, use a 60s floor (matches other modules),
                # unless the server sends a larger Retry-After.
                floor_s = 60.0 if response.status_code == 429 else 0.0
                sleep_s = max(backoff_s + jitter_s, retry_after_s or 0, floor_s)

                _log(
                    f"⚠ Delete app rate-limited/transient error ({response.status_code}). "
                    f"Retrying in {sleep_s:.1f}s (attempt {attempt}/{max_attempts})"
                )
                time.sleep(sleep_s)
                continue

            response.raise_for_status()
            _log(f"✓ Deleted application: {display_name} (ID: {app_id})")
            return True

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 404:
                _log(f"⚠ Application {display_name} not found (404), skipping...")
                return True
            if status == 400:
                _log(f"⚠ Cannot delete {display_name} - likely the default application (400)")
                return False

            if status in (429, 500, 502, 503, 504) and attempt < max_attempts:
                backoff_s = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
                jitter_s = random.uniform(0, 0.25 * backoff_s)
                sleep_s = backoff_s + jitter_s
                _log(
                    f"⚠ Delete app failed ({status}). Retrying in {sleep_s:.1f}s "
                    f"(attempt {attempt}/{max_attempts})"
                )
                time.sleep(sleep_s)
                continue

            _log(f"✗ Failed to delete application {display_name}: {e}")
            return False

        except requests.exceptions.RequestException as e:
            if attempt == max_attempts:
                _log(f"✗ Failed to delete application {display_name}: {e}")
                return False
            backoff_s = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            jitter_s = random.uniform(0, 0.25 * backoff_s)
            sleep_s = backoff_s + jitter_s
            _log(
                f"⚠ Network error deleting {display_name}. Retrying in {sleep_s:.1f}s "
                f"(attempt {attempt}/{max_attempts})"
            )
            time.sleep(sleep_s)

    return False

def get_prehooks(token):
    """Fetches all prehook configurations using the vendor token."""
    url = f"{BASE_URL}/prehooks/resources/configurations/v1"
    headers = {
        'Authorization': f'Bearer {token}',
        'frontegg-environment-id': CLIENT_ID
    }
    try:
        response = make_request_with_rate_limiting("GET", url, headers=headers)
        prehooks = response.json()
        _log(f"Retrieved {len(prehooks)} prehook(s).")
        return prehooks
    except requests.exceptions.HTTPError as e:
        _log(f"Failed to get prehooks: {e}")
        return []

def delete_prehook(token, prehook_id, prehook_name=None):
    """Deletes a prehook by ID using the vendor token."""
    url = f"{BASE_URL}/prehooks/resources/configurations/v1/{prehook_id}"
    headers = {
        'Authorization': f'Bearer {token}',
        'frontegg-environment-id': CLIENT_ID
    }
    display_name = prehook_name if prehook_name else prehook_id
    try:
        make_request_with_rate_limiting("DELETE", url, headers=headers)
        _log(f"✓ Deleted prehook: {display_name} (ID: {prehook_id})")
        return True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            _log(f"⚠ Prehook {display_name} not found (404), skipping...")
        else:
            _log(f"✗ Failed to delete prehook {display_name}: {e}")
        return False

def main():
    token = get_vendor_token()

    # Execute deletion processes based on flags
    if DELETE_APPLICATIONS:
        _log("\n=== Deleting Applications ===")
        
        # Step 1: Create a dummy application first
        _log("Step 1: Creating dummy application to enable full deletion...")
        dummy_app = create_dummy_application(token)
        
        if not dummy_app:
            _log("⚠ Warning: Could not create dummy app, some applications may not be deletable")
        
        # Step 2: Get all applications (including the dummy we just created)
        _log("\nStep 2: Fetching all applications...")
        applications = get_applications(token)
        
        if not applications:
            _log("No applications found to delete.")
        else:
            # Separate dummy app from others
            dummy_app_id = dummy_app['id'] if dummy_app else None
            apps_to_delete = []
            
            for app in applications:
                app_name = app.get('name', 'Unknown')
                is_default = app.get('isDefault', False)
                
                # We'll delete the dummy app last
                if app['id'] == dummy_app_id:
                    _log(f"ℹ Will delete dummy app '{app_name}' last")
                else:
                    apps_to_delete.append(app)
                    if is_default:
                        _log(f"ℹ Found default application: {app_name}")
            
            # Step 3: Delete all non-dummy applications
            _log(f"\nStep 3: Deleting {len(apps_to_delete)} original applications...")
            deleted_count = 0
            failed_count = 0
            
            for app in apps_to_delete:
                if delete_application(token, app['id'], app.get('name')):
                    deleted_count += 1
                else:
                    failed_count += 1
            
            # Step 4: Try to delete the dummy app (this might fail if it's now the default)
            if dummy_app:
                _log("\nStep 4: Attempting to delete dummy application...")
                if delete_application(token, dummy_app['id'], dummy_app.get('name')):
                    deleted_count += 1
                    _log("✓ Successfully cleaned up dummy application")
                else:
                    _log("ℹ Dummy app remains (now default) - you may want to delete it manually")
            
            _log(f"\n📊 Summary: Deleted {deleted_count}/{len(applications)} applications")
            if failed_count > 0:
                _log(f"   ({failed_count} could not be deleted)")
    
    if DELETE_TENANTS:
        _log("\n=== Deleting Tenants ===")
        tenant_ids = get_tenant_ids(token)
        for tenant_id in tenant_ids:
            delete_tenant(token, tenant_id)

    if DELETE_USERS:
        _log("\n=== Deleting Users ===")
        users = get_all_users(token)
        for user in users:
            delete_user(token, user["id"])

    if DELETE_PERMISSIONS:
        _log("\n=== Deleting Permissions ===")
        permissions = get_permissions(token)
        deleted_count = 0
        cascaded_count = 0
        failed_count = 0
        for permission_id in permissions:
            result = delete_permission(token, permission_id)
            if result == "deleted":
                deleted_count += 1
                _log(f"Deleted permission with ID: {permission_id}")
            elif result == "missing":
                # Already gone (cascade/system) — collapsed into the summary
                # instead of one log line per ID.
                cascaded_count += 1
            else:
                failed_count += 1

        summary = f"Deleted {deleted_count}/{len(permissions)} permissions"
        if cascaded_count:
            summary += f"; {cascaded_count} already removed (cascade/system)"
        if failed_count:
            summary += f"; {failed_count} failed"
        _log(f"\n📊 Summary: {summary}")

    if DELETE_ROLES:
        _log("\n=== Deleting Roles ===")
        roles = get_roles(token)
        for role_id in roles:
            delete_role(token, role_id)
    
    if DELETE_PREHOOKS:
        _log("\n=== Deleting Prehooks ===")
        prehooks = get_prehooks(token)
        if not prehooks:
            _log("No prehooks found to delete.")
        else:
            deleted_count = 0
            for prehook in prehooks:
                prehook_name = prehook.get('displayName', 'Unknown')
                prehook_id = prehook.get('id')
                if delete_prehook(token, prehook_id, prehook_name):
                    deleted_count += 1
            _log(f"\n📊 Summary: Deleted {deleted_count}/{len(prehooks)} prehooks")

if __name__ == "__main__":
    main()
