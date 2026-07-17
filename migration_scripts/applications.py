import json
import time
from datetime import datetime
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection
from migration_scripts.tenants import get_tenants

# Rate limit configuration
DEFAULT_RATE_LIMIT = 30
last_request_times = {}

# Application access types (Frontegg Multi-Apps)
FREE_ACCESS = "FREE_ACCESS"
MANAGED_ACCESS = "MANAGED_ACCESS"

def get_headers(client):
    """Generate headers with the client token."""
    return {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json"
    }

def get_headers_with_tenant(client, tenant_id):
    """Headers scoped to a specific tenant (required by per-tenant endpoints)."""
    headers = get_headers(client)
    headers["frontegg-tenant-id"] = tenant_id
    return headers

def enforce_rate_limit(endpoint, rate_limit=DEFAULT_RATE_LIMIT):
    """Enforce the rate limit for a specific endpoint."""
    interval = 60 / rate_limit
    
    if endpoint in last_request_times:
        time_since_last_request = time.time() - last_request_times[endpoint]
        if time_since_last_request < interval:
            time.sleep(interval - time_since_last_request)
    
    last_request_times[endpoint] = time.time()

def make_request_with_rate_limiting(method, url, client, headers=None, params=None, json_data=None):
    """Handle rate-limited requests."""
    enforce_rate_limit(url)
    try:
        response = client.session.request(method, url, headers=headers, params=params, json=json_data)
        if response.status_code == 429:
            log_warning("⚠ Rate limit exceeded. Retrying after delay...")
            time.sleep(60)
            response = client.session.request(method, url, headers=headers, params=params, json=json_data)
        response.raise_for_status()
        return response
    except Exception as e:
        log_error(f"Request failed: {e}")
        raise

def get_user_id_by_email(client, email, tenant_id):
    """Fetch a user's ID by email within a tenant (returns None if not found)."""
    endpoint = f"{client.base_url}/identity/resources/users/v3?_email={email}"
    headers = get_headers_with_tenant(client, tenant_id)
    try:
        response = make_request_with_rate_limiting("GET", endpoint, client, headers=headers)
        users = response.json().get("items", [])
        return users[0].get('id') if users else None
    except Exception:
        return None

def get_applications(client):
    """Fetch all applications from the client."""
    logger = get_logger()
    logger.debug("Fetching applications")
    
    endpoint = f"{client.base_url}/applications/resources/applications/v1?_excludeAgents=true"
    headers = get_headers(client)
    
    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        applications = response.json()
        log_success(f"Retrieved {len(applications)} applications")
        return applications
    except Exception as e:
        log_error(f"Error fetching applications: {e}")
        return []

def create_application(client, app_data):
    """Create an application in the destination."""
    logger = get_logger()
    app_name = app_data.get('name', 'Unknown')
    logger.debug(f"Creating application: {app_name}")
    
    endpoint = f"{client.base_url}/applications/resources/applications/v1"
    headers = get_headers(client)
    
    # Prepare the request body - remove fields that shouldn't be sent in creation
    req_body = {
        'name': app_data['name'],
        'appURL': app_data.get('appURL', ''),
        'loginURL': app_data.get('loginURL', ''),
        'accessType': app_data.get('accessType', 'FREE_ACCESS'),
        'isActive': app_data.get('isActive', True),
        'type': app_data.get('type', 'WEB'),
        'frontendStack': app_data.get('frontendStack', 'REACT'),
        'isDefault': app_data.get('isDefault', False)  # IMPORTANT: Preserve default flag
    }
    
    # Add optional fields if they exist
    if app_data.get('logoURL'):
        req_body['logoURL'] = app_data['logoURL']
    if app_data.get('description'):
        req_body['description'] = app_data['description']
    if app_data.get('metadata'):
        req_body['metadata'] = app_data['metadata']
    
    # Log if this is a default app
    if app_data.get('isDefault', False):
        logger.info(f"📌 Setting {app_name} as DEFAULT application")
    
    try:
        response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=req_body)
        created_app = response.json()
        log_success(f"✓ Created application: {app_name} (ID: {created_app.get('id', 'N/A')})")
        return created_app
    except Exception as e:
        log_error(f"✗ Failed to create application '{app_name}': {e}")
        return None

def delete_application(client, app_id, app_name=None):
    """Delete an application."""
    logger = get_logger()
    display_name = app_name if app_name else app_id
    logger.debug(f"Deleting application: {display_name}")
    
    endpoint = f"{client.base_url}/applications/resources/applications/v1/{app_id}"
    headers = get_headers(client)
    
    try:
        response = make_request_with_rate_limiting('DELETE', endpoint, client, headers=headers)
        log_success(f"✓ Deleted application: {display_name}")
        return True
    except Exception as e:
        log_error(f"✗ Failed to delete application '{display_name}': {e}")
        return False

def update_application(client, app_id, app_data):
    """Update an existing application."""
    logger = get_logger()
    app_name = app_data.get('name', 'Unknown')
    logger.debug(f"Updating application: {app_name}")
    
    endpoint = f"{client.base_url}/applications/resources/applications/v1/{app_id}"
    headers = get_headers(client)
    
    # Prepare update body - only include fields that can be updated
    update_body = {
        'name': app_data['name'],
        'appURL': app_data.get('appURL', ''),
        'loginURL': app_data.get('loginURL', ''),
        'accessType': app_data.get('accessType', 'FREE_ACCESS'),
        'isActive': app_data.get('isActive', True),
        'type': app_data.get('type', 'WEB'),
        'frontendStack': app_data.get('frontendStack', 'REACT')
    }
    
    if app_data.get('logoURL'):
        update_body['logoURL'] = app_data['logoURL']
    if app_data.get('description'):
        update_body['description'] = app_data['description']
    if app_data.get('metadata'):
        update_body['metadata'] = app_data['metadata']
    
    try:
        response = make_request_with_rate_limiting('PUT', endpoint, client, headers=headers, json_data=update_body)
        log_success(f"✓ Updated application: {app_name}")
        return response.json()
    except Exception as e:
        log_error(f"✗ Failed to update application '{app_name}': {e}")
        return None

def get_app_tenant_access_type(client, app_id, tenant_id):
    """Return the per-app accessType ('FREE_ACCESS' / 'MANAGED_ACCESS') for an
    application on a specific tenant, or None when the app is not assigned to it.

    Uses the v2 tenant-assignments endpoint, which reports per-app accessType.
    """
    endpoint = f"{client.base_url}/applications/resources/applications/tenant-assignments/v2"
    headers = get_headers_with_tenant(client, tenant_id)
    params = {"appIds": app_id, "tenantIds": tenant_id}

    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers, params=params)
        data = response.json()
    except Exception as e:
        log_error(f"✗ Failed to read tenant assignment (app {app_id} / tenant {tenant_id}): {e}")
        return None

    # Response shape: [{ "tenantId": ..., "assignments": [{ "appId": ..., "accessType": ... }] }]
    for entry in data if isinstance(data, list) else []:
        if entry.get("tenantId") != tenant_id:
            continue
        for assignment in entry.get("assignments", []):
            if assignment.get("appId") == app_id:
                return assignment.get("accessType")
    return None

def assign_app_to_tenant(client, app_id, tenant_id, access_type):
    """Assign an application to a tenant in the destination with the given accessType."""
    endpoint = f"{client.base_url}/applications/resources/applications/tenant-assignments/v1/{app_id}"
    headers = get_headers(client)
    req_body = {"tenantId": tenant_id, "accessType": access_type}

    try:
        make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=req_body)
        log_success(f"✓ Assigned app {app_id} to tenant {tenant_id} ({access_type})")
        return True
    except Exception as e:
        log_error(f"✗ Failed to assign app {app_id} to tenant {tenant_id}: {e}")
        return False

def get_users_assigned_to_app(client, app_id, tenant_id):
    """Return the users assigned to an application within a tenant (source side)."""
    endpoint = f"{client.base_url}/identity/resources/applications/v1/{app_id}/users"
    headers = get_headers_with_tenant(client, tenant_id)

    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        data = response.json()
    except Exception as e:
        log_error(f"✗ Failed to fetch users for app {app_id} / tenant {tenant_id}: {e}")
        return []

    if isinstance(data, dict):
        return data.get("items") or data.get("users") or []
    return data if isinstance(data, list) else []

def assign_users_to_app(client, app_id, tenant_id, user_ids):
    """Assign a set of users to an application within a tenant (destination side)."""
    if not user_ids:
        return True

    endpoint = f"{client.base_url}/identity/resources/applications/v1"
    headers = get_headers_with_tenant(client, tenant_id)
    req_body = {"appId": app_id, "tenantId": tenant_id, "userIds": user_ids}

    try:
        make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=req_body)
        log_success(f"✓ Assigned {len(user_ids)} user(s) to app {app_id} on tenant {tenant_id}")
        return True
    except Exception as e:
        log_error(f"✗ Failed to assign users to app {app_id} on tenant {tenant_id}: {e}")
        return False

def migrate_app_tenant_and_user_assignments(source_client, destination_client, source_app, dest_app_id, source_tenants):
    """Replicate a MANAGED_ACCESS application's tenant and user assignments.

    For each source tenant:
      - Read the per-app accessType on that tenant (v2). Skip tenants the app
        is not assigned to.
      - Recreate the app→tenant assignment in the destination with the same
        accessType.
      - FREE_ACCESS on the tenant means every tenant user has access, so no
        per-user assignment is needed.
      - MANAGED_ACCESS on the tenant means access is explicit, so copy the
        per-user assignments (mapping source users to destination users by
        email within the same tenant).
    """
    logger = get_logger()
    source_app_id = source_app.get('id')
    app_name = source_app.get('name', 'Unknown')

    tenants_assigned = 0
    users_assigned = 0

    for tenant in source_tenants:
        tenant_id = tenant.get('tenantId')
        if not tenant_id:
            continue

        access_type = get_app_tenant_access_type(source_client, source_app_id, tenant_id)
        if not access_type:
            # App is not assigned to this tenant in the source — nothing to copy.
            continue

        if assign_app_to_tenant(destination_client, dest_app_id, tenant_id, access_type):
            tenants_assigned += 1

        if access_type == FREE_ACCESS:
            # Tenant-wide access; no explicit user assignments to migrate.
            continue

        # MANAGED_ACCESS on the tenant: migrate the explicit user assignments.
        source_users = get_users_assigned_to_app(source_client, source_app_id, tenant_id)
        if not source_users:
            continue

        dest_user_ids = []
        for user in source_users:
            email = user.get('email')
            if not email:
                log_warning(f"⚠ Skipping user without email on tenant {tenant_id} (cannot map across accounts)")
                continue
            dest_user_id = get_user_id_by_email(destination_client, email, tenant_id)
            if dest_user_id:
                dest_user_ids.append(dest_user_id)
            else:
                log_warning(f"⚠ No destination user for '{email}' in tenant {tenant_id}; skipping app assignment")

        if assign_users_to_app(destination_client, dest_app_id, tenant_id, dest_user_ids):
            users_assigned += len(dest_user_ids)

    logger.print_stats(f"Assignments migrated for '{app_name}'", {
        "Tenants assigned": tenants_assigned,
        "Users assigned": users_assigned,
    })

def migrate_applications(source_client, destination_client):
    """Migrate all applications from source to destination."""
    logger = get_logger()
    logger.info("🚀 Starting applications migration process")
    
    # Fetch applications from source
    source_applications = get_applications(source_client)
    if not source_applications:
        log_warning("No applications found to migrate")
        return
    
    # Separate default and non-default applications
    source_default_app = None
    non_default_apps = []
    
    for app in source_applications:
        if app.get('isDefault', False):
            source_default_app = app
            logger.info(f"Found source default application: {app.get('name', 'Unknown')}")
        else:
            non_default_apps.append(app)
    
    # Fetch existing applications from destination
    log_subsection("Analyzing Destination Applications")
    dest_applications = get_applications(destination_client)
    dest_default_app = None
    
    for app in dest_applications:
        if app.get('isDefault', False):
            dest_default_app = app
            logger.info(f"Found destination default application: {app.get('name', 'Unknown')} (ID: {app.get('id')})")
            break
    
    # Display migration plan
    logger.print_summary([
        f"Source applications: {len(source_applications)} ({len(non_default_apps)} non-default, {'1 default' if source_default_app else 'no default'})",
        f"Destination applications to remove: {len(dest_applications)}",
        f"Step 1: Migrate {len(non_default_apps)} non-default apps from source",
        f"Step 2: Delete ALL {len(dest_applications)} destination apps",
        f"Step 3: Migrate source default app"
    ], "Migration Plan")
    
    created_count = 0
    failed_count = 0
    # Maps each SOURCE application id to its newly created DESTINATION id so we
    # can replicate assignments (which reference destination app ids) afterwards.
    app_id_map = {}
    
    # Step 1: Migrate all non-default applications
    if non_default_apps:
        log_subsection("Step 1: Migrating Non-Default Applications")
        progress, task = logger.start_progress(len(non_default_apps), "Creating non-default applications")
        
        for app in non_default_apps:
            app_name = app.get('name', 'Unknown')
            logger.update_progress(1, f"Creating: {app_name}")
            
            result = create_application(destination_client, app)
            if result:
                created_count += 1
                if app.get('id') and result.get('id'):
                    app_id_map[app['id']] = result['id']
            else:
                failed_count += 1
        
        logger.stop_progress()
        
        logger.print_stats("Non-Default Applications Migration", {
            "Total": len(non_default_apps),
            "Successfully Created": created_count,
            "Failed": failed_count
        })
    
    # Step 2: Delete ALL destination applications (including any leftovers)
    if dest_applications and source_default_app:
        log_subsection("Step 2: Removing ALL Destination Applications")
        
        for app in dest_applications:
            app_name = app.get('name', 'Unknown')
            logger.info(f"Deleting destination app: {app_name}")
            delete_success = delete_application(
                destination_client, 
                app['id'], 
                app_name
            )
            
            if not delete_success:
                log_warning(f"Failed to delete {app_name}. Continuing anyway...")
    
    # Step 3: Migrate source default application
    if source_default_app:
        log_subsection("Step 3: Migrating Source Default Application")
        logger.info(f"Creating default application: {source_default_app.get('name', 'Unknown')}")
        
        result = create_application(destination_client, source_default_app)
        if result:
            created_count += 1
            if source_default_app.get('id') and result.get('id'):
                app_id_map[source_default_app['id']] = result['id']
            log_success(f"✅ Successfully migrated default application: {source_default_app.get('name')}")
        else:
            failed_count += 1
            log_error(f"❌ Failed to migrate default application: {source_default_app.get('name')}")
    
    # Step 4: Replicate tenant & user assignments for MANAGED_ACCESS applications.
    # FREE_ACCESS apps need nothing beyond creation (everyone has access).
    managed_apps = [
        app for app in source_applications
        if app.get('accessType') == MANAGED_ACCESS and app.get('id') in app_id_map
    ]
    if managed_apps:
        log_subsection("Step 4: Migrating MANAGED_ACCESS Tenant & User Assignments")
        source_tenants = get_tenants(source_client)
        for app in managed_apps:
            logger.info(f"Copying assignments for MANAGED_ACCESS app: {app.get('name', 'Unknown')}")
            migrate_app_tenant_and_user_assignments(
                source_client,
                destination_client,
                app,
                app_id_map[app['id']],
                source_tenants,
            )
    
    # Final summary
    logger.print_stats("Applications Migration Summary", {
        "Total Source Applications": len(source_applications),
        "Applications Created": created_count,
        "Applications Failed": failed_count,
        "Default App Replaced": "Yes" if source_default_app else "No"
    })
    
    log_success("✅ Applications migration completed successfully!")