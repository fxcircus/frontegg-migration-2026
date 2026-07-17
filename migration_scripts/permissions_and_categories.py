import json
import requests
from utility.utils import log
from migration_scripts.tenants import make_request_with_rate_limiting, get_headers

def _is_fe_permission_key(permission_key):
    return isinstance(permission_key, str) and permission_key.lower().startswith("fe.")

def log_detailed_api_call(method, url, headers=None, data=None):
    """Log all details of the API call."""
    log(f"API CALL: {method} {url}")
    if headers:
        # Avoid leaking bearer tokens into logs
        safe_headers = dict(headers)
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = "Bearer [REDACTED]"
        log(f"Headers: {json.dumps(safe_headers, indent=2)}")
    if data:
        log(f"Data Payload: {json.dumps(data, indent=2)}")

def get_categories(client):
    log("Getting categories from account.")
    endpoint = client.base_url + '/identity/resources/permissions/v1/categories'
    headers = get_headers(client)
    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        categories = response.json()
        
        # Structure the categories as expected
        formatted_categories = [
            {
                "id": cat["id"],
                "name": cat["name"],
                "description": cat.get("description", ""),
                "createdAt": cat.get("createdAt", ""),
                "feCategory": cat.get("feCategory", False),
            }
            for cat in categories
        ]
        
        log(f"Retrieved {len(formatted_categories)} categories.")
        return formatted_categories
    except Exception as e:
        log(f"Error fetching categories: {e}")
        return []

def create_categories(client, categories):
    log("Creating categories in destination account.")
    endpoint = client.base_url + '/identity/resources/permissions/v1/categories'
    headers = get_headers(client)
    category_mapping = {}

    for category in categories:
        category_json = {
            'name': category['name'],
            'description': category.get('description', ''),
        }
        try:
            response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=category_json)
            response_data = response.json()
            destination_category_id = response_data["id"]
            category_mapping[category["id"]] = destination_category_id
            log(f"Category '{category['name']}' created with ID: {destination_category_id}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None:
                error_content = e.response.json()
                if any("already exist" in error.lower() for error in error_content.get('errors', [])):
                    log(f"Category '{category['name']}' already exists. Skipping.")
                else:
                    raise
            else:
                raise
    log("Categories creation completed.")
    return category_mapping

def get_permissions(client):
    log("Getting permissions from source account.")
    endpoint = client.base_url + '/identity/resources/permissions/v1'
    headers = get_headers(client)
    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        permissions = response.json()
        log(f"Retrieved {len(permissions)} permissions.")
        return permissions
    except Exception as e:
        log(f"Error fetching permissions: {e}")
        return []

def create_permissions(client, permissions, batch_size=100):
    log("Creating permissions in destination account.")
    endpoint = client.base_url + '/identity/resources/permissions/v1'
    headers = get_headers(client)

    # The Frontegg API expects the body to be an ARRAY.
    created = 0
    skipped_existing = 0
    failed = 0
    valid_permissions = []

    for permission in permissions:
        key = permission.get("key")
        name = permission.get("name")
        category_id = permission.get("categoryId")
        if not key or not name or not category_id:
            continue

        valid_permissions.append(
            {
                "key": key,
                "name": name,
                "description": permission.get("description") or "",
                "categoryId": category_id,
            }
        )

    if not valid_permissions:
        log("Permissions creation completed. No valid permissions to create.")
        return

    # Safety fallback to avoid invalid batching configs.
    batch_size = max(int(batch_size or 1), 1)

    def _create_single_permission(single_permission):
        nonlocal created, skipped_existing, failed
        key = single_permission.get("key", "<unknown>")
        payload = [single_permission]
        try:
            make_request_with_rate_limiting(
                "POST",
                endpoint,
                client,
                headers=headers,
                json_data=payload,
            )
            created += 1
        except requests.exceptions.HTTPError as e:
            if e.response is None:
                failed += 1
                return

            try:
                error_content = e.response.json()
            except Exception:
                error_content = {"raw": e.response.text}

            errors = error_content.get("errors") if isinstance(error_content, dict) else None
            errors_text = " ".join(str(x) for x in errors) if isinstance(errors, list) else str(error_content)

            if "already exist" in errors_text.lower() or "already exists" in errors_text.lower():
                skipped_existing += 1
                return

            failed += 1
            log(f"Error creating permission '{key}'. Response content:\n{json.dumps(error_content, indent=2)}")

    for start in range(0, len(valid_permissions), batch_size):
        batch = valid_permissions[start:start + batch_size]
        log_detailed_api_call(
            "POST",
            endpoint,
            headers=headers,
            data={"permissionsCount": len(batch)}
        )
        try:
            make_request_with_rate_limiting(
                "POST",
                endpoint,
                client,
                headers=headers,
                json_data=batch,
            )
            created += len(batch)
            log(
                f"Created permissions batch {start // batch_size + 1} "
                f"({len(batch)} permissions)."
            )
        except requests.exceptions.HTTPError as e:
            # If a whole batch fails (e.g., one duplicate), fallback to per-item creation.
            log(
                f"Batch creation failed for batch {start // batch_size + 1}. "
                "Retrying permissions one by one for this batch."
            )
            if e.response is not None:
                try:
                    batch_error = e.response.json()
                except Exception:
                    batch_error = {"raw": e.response.text}
                log(f"Batch error response:\n{json.dumps(batch_error, indent=2)}")
            for single_permission in batch:
                _create_single_permission(single_permission)

    log(
        f"Permissions creation completed. Created={created}, Skipped(existing)={skipped_existing}, Failed={failed}"
    )
    
def migrate_settings(source_client, destination_client, migrate_categories, migrate_permissions):
    """Orchestrate the migration of categories and permissions."""
    log("Starting settings migration.")
    
    # Step 1: Fetch categories from source and destination
    source_categories = get_categories(source_client)
    log(f"Retrieved {len(source_categories)} categories from source.")
    
    destination_categories = get_categories(destination_client)
    log(f"Retrieved {len(destination_categories)} categories from destination.")
    
    # Step 2: Map source categories to destination categories based on name and description
    category_mapping = {}
    for src_cat in source_categories:
        matching_dest_cat = next(
            (dest_cat for dest_cat in destination_categories
             if dest_cat['name'] == src_cat['name'] and dest_cat.get('description', '') == src_cat.get('description', '')),
            None
        )
        if matching_dest_cat:
            category_mapping[src_cat['id']] = matching_dest_cat['id']

    log(f"Category mapping completed with {len(category_mapping)} mapped categories.")

    # Step 3: Create categories if enabled and not already existing in the destination
    if migrate_categories:
        categories_to_create = [cat for cat in source_categories if cat['id'] not in category_mapping]
        if categories_to_create:
            new_category_mapping = create_categories(destination_client, categories_to_create)
            category_mapping.update(new_category_mapping)
        log(f"{len(categories_to_create)} categories created in the destination account.")

    # Step 4: Migrate Permissions if enabled
    if migrate_permissions:
        source_permissions = get_permissions(source_client)
        log(f"Source permissions retrieved for migration: {len(source_permissions)} permissions.")
        destination_permissions = get_permissions(destination_client)
        destination_permission_keys = {
            perm.get("key")
            for perm in destination_permissions
            if isinstance(perm, dict) and perm.get("key")
        }
        log(f"Destination permissions retrieved for de-duplication: {len(destination_permission_keys)} permission keys.")
        
        # Step 5: Assign the correct categoryId from destination to each permission
        permissions_to_migrate = []
        fe_permissions_skipped_existing = 0
        for permission in source_permissions:
            permission_key = permission.get("key")
            if _is_fe_permission_key(permission_key) and permission_key in destination_permission_keys:
                fe_permissions_skipped_existing += 1
                continue

            dest_category_id = category_mapping.get(permission['categoryId'])
            if dest_category_id:
                permission_to_migrate = dict(permission)
                permission_to_migrate['categoryId'] = dest_category_id
                permissions_to_migrate.append(permission_to_migrate)
            else:
                log(f"Destination category mapping for permission '{permission['name']}' with source category ID '{permission['categoryId']}' not found. Skipping.")

        # Step 6: Create permissions with mapped category IDs
        if permissions_to_migrate:
            if fe_permissions_skipped_existing:
                log(f"Skipped {fe_permissions_skipped_existing} 'fe.' permissions because they already exist in destination.")
            log(f"{len(permissions_to_migrate)} permissions matched with categories for migration.")
            create_permissions(destination_client, permissions_to_migrate)
        else:
            log("No permissions with valid categories to migrate from the source.")
    else:
        log("Skipping permissions migration as per configuration.")

    log("Settings migration completed.")
