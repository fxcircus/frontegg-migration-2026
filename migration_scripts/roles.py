import json
import requests
from utility.utils import log
from migration_scripts.tenants import make_request_with_rate_limiting
from migration_scripts.permissions_and_categories import get_permissions
import time

def log_detailed_api_call(method, url, headers=None, data=None):
    """Log all details of the API call."""
    log(f"API CALL: {method} {url}")
    if headers:
        # Avoid leaking bearer tokens into logs
        safe_headers = dict(headers)
        for auth_key in ("authorization", "Authorization"):
            if auth_key in safe_headers:
                safe_headers[auth_key] = "Bearer [REDACTED]"
        log(f"Headers: {json.dumps(safe_headers, indent=2)}")
    if data:
        log(f"Data Payload: {json.dumps(data, indent=2)}")

def get_roles(client, split=True):
    """
    Fetch roles from the account using the v2 endpoint.
    
    If split is True (default), returns a tuple:
      (roles_with_tenant, roles_without_tenant)
    Otherwise, returns the full list of roles.
    """
    log("Fetching roles from account (using v2 endpoint).")
    endpoint = client.base_url + '/identity/resources/roles/v2?_limit=2000'
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json'
    }
    try:
        log_detailed_api_call("GET", endpoint, headers=headers)
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        data = response.json()
        
        # Extract roles from the 'items' key
        roles = data.get("items", [])
        log(f"Retrieved {len(roles)} roles from the v2 endpoint.")
        
        # Log each retrieved role for detailed inspection
        for role in roles:
            log(f"Role: {json.dumps(role, indent=2)}")
            
        if not split:
            return roles
        
        roles_with_tenant = []
        roles_without_tenant = []
        for role in roles:
            if role.get('tenantId'):
                roles_with_tenant.append(role)
            else:
                roles_without_tenant.append(role)
        return roles_with_tenant, roles_without_tenant
    except Exception as e:
        log(f"Error fetching roles: {e}")
        return [] if not split else ([], [])

def create_roles(client, roles_with_tenant, roles_without_tenant):
    log("Creating roles")
    endpoint = client.base_url + '/identity/resources/roles/v1'
    
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json'
    }

    role_id_mapping = {}  # Mapping from source role ID to destination role ID

    # Prepare list of role data for roles without tenantId (batch creation)
    roles_data_without_tenant = []
    for role in roles_without_tenant:
        role_data = {
            'name': role['name'],
            'key': role['key'],
            'description': role.get('description', ''),
            'isDefault': role.get('isDefault', False),
            'level': role['level'],
        }
        roles_data_without_tenant.append(role_data)

    log(f"Sending array of {len(roles_data_without_tenant)} roles to create without tenantId.")

    if len(roles_data_without_tenant) == 0:
        log("No new roles to create without tenantId. Skipping API call.")
    else:
        log_detailed_api_call("POST", endpoint, headers=headers, data=roles_data_without_tenant)

        try:
            response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=roles_data_without_tenant)
            response_data = response.json()

            for original_role, created_role in zip(roles_without_tenant, response_data):
                role_id_mapping[original_role["id"]] = created_role["id"]
                log(f"Role '{original_role['name']}' created with ID: {created_role['id']}")

        except requests.exceptions.HTTPError as e:
            log(f"Error creating roles without tenantId: {e}")
            if e.response is not None:
                try:
                    error_content = e.response.json()
                except ValueError:
                    log(f"Error response content:\n{e.response.text}")
                    raise
                log(f"Error response content:\n{json.dumps(error_content, indent=2)}")
                errors = error_content.get('errors', []) if isinstance(error_content, dict) else []
                if any("already exist" in str(error).lower() for error in errors):
                    log("Some roles already exist. Skipping those.")
                else:
                    raise
            else:
                raise

    # Handle roles with tenantId individually
    for role in roles_with_tenant:
        role_data = {
            'name': role['name'],
            'key': role['key'],
            'description': role.get('description', ''),
            'isDefault': role.get('isDefault', False),
            'tenantId': role['tenantId'],
            'level': role['level'],
        }
        headers_with_tenant = headers.copy()
        headers_with_tenant['frontegg-tenant-id'] = role['tenantId']

        log(f"Creating role '{role['name']}' with tenantId '{role['tenantId']}'.")
        log_detailed_api_call("POST", endpoint, headers=headers_with_tenant, data=[role_data])

        try:
            response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers_with_tenant, json_data=[role_data])
            created_roles = response.json()
            for created_role in created_roles:
                role_id_mapping[role["id"]] = created_role["id"]
                log(f"Role '{role['name']}' created with ID: {created_role['id']}")

        except requests.exceptions.HTTPError as e:
            log(f"Error creating role '{role['name']}' with tenantId: {e}")
            if e.response is not None:
                try:
                    error_content = e.response.json()
                except ValueError:
                    log(f"Error response content:\n{e.response.text}")
                    raise
                log(f"Error response content for role '{role['name']}' with tenantId '{role['tenantId']}':\n{json.dumps(error_content, indent=2)}")
                errors = error_content.get('errors', []) if isinstance(error_content, dict) else []
                if any("already exist" in str(error).lower() for error in errors):
                    log("Role already exists. Skipping.")
                else:
                    raise
            else:
                raise
        except Exception as e:
            log(f"Error creating role '{role['name']}' with tenantId: {e}")
            raise

    log("Roles creation completed.")
    return role_id_mapping

def assign_permissions_to_roles(client, roles, role_id_mapping, dest_permissions_by_key):
    log("Assigning permissions to roles")
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json'
    }

    for role in roles:
        dest_role_id = role_id_mapping.get(role['id'])
        if not dest_role_id:
            log(f"No matching destination role ID for role '{role['name']}'. Skipping permission assignment.")
            continue

        # Map source permission IDs to destination permission IDs
        permission_ids = []
        for perm_id in role.get('permissions', []):
            source_permission = next((p for p in role.get('permissionsData', []) if p['id'] == perm_id), None)
            if source_permission:
                perm_key = source_permission['key']
                dest_perm_id = dest_permissions_by_key.get(perm_key)
                if dest_perm_id:
                    permission_ids.append(dest_perm_id)
                else:
                    log(f"Permission key '{perm_key}' not found in destination. Skipping.")
            else:
                log(f"Permission ID '{perm_id}' not found in source permissions.")

        if permission_ids:
            assign_endpoint = f"{client.base_url}/identity/resources/roles/v1/{dest_role_id}/permissions"
            assign_data = {"permissionIds": permission_ids}
            headers_with_tenant = headers.copy()
            if role.get('tenantId'):
                headers_with_tenant['frontegg-tenant-id'] = role['tenantId']

            try:
                response = make_request_with_rate_limiting('PUT', assign_endpoint, client, headers=headers_with_tenant, json_data=assign_data)
                log(f"Permissions assigned to role '{role['name']}' (ID: {dest_role_id}) with status: {response.status_code}")
            except requests.exceptions.HTTPError as e:
                log(f"Error assigning permissions to role '{role['name']}' (ID: {dest_role_id}): {e}")
                if e.response is not None:
                    try:
                        log(f"Error response content:\n{json.dumps(e.response.json(), indent=2)}")
                    except ValueError:
                        log(f"Error response content:\n{e.response.text}")
                else:
                    raise
            except Exception as e:
                log(f"Unexpected error during permission assignment for role '{role['name']}': {e}")
                raise
        else:
            log(f"No permissions to assign for role '{role['name']}'.")

    log("Permissions assignment to roles completed.")

def migrate_roles(source_client, destination_client):
    log("Starting roles migration")
    # Get roles from source and destination (split by tenant)
    source_roles_with_tenant, source_roles_without_tenant = get_roles(source_client)
    source_permissions = get_permissions(source_client)
    dest_roles_with_tenant, dest_roles_without_tenant = get_roles(destination_client)
    dest_permissions = get_permissions(destination_client)

    source_permissions_by_id = {p['id']: p for p in source_permissions}
    dest_permissions_by_key = {p['key']: p['id'] for p in dest_permissions}

    def role_scope_key(role):
        """Identify a role by key + scope so the same key can exist per tenant.

        tenantId=None is the account-level ("global"/Available to All) scope.
        Tenant-scoped roles wire to the same tenant in the destination because
        tenants are migrated with their original IDs.
        """
        return (role.get('key'), role.get('tenantId') or None)

    dest_role_scope_keys = {
        role_scope_key(role)
        for role in (dest_roles_with_tenant + dest_roles_without_tenant)
    }

    unique_roles = {}
    # Merge both categories from source and filter out roles that already exist
    for role in (source_roles_with_tenant + source_roles_without_tenant):
        scope_key = role_scope_key(role)
        role_key, role_tenant_id = scope_key
        tenant_label = role_tenant_id if role_tenant_id else "global"

        if scope_key in dest_role_scope_keys:
            log(f"Role '{role_key}' in scope '{tenant_label}' already exists in destination. Skipping.")
            continue
        if scope_key in unique_roles:
            log(f"Duplicate role found for key '{role_key}' in scope '{tenant_label}'. Skipping duplicate.")
            continue
        role_permissions_data = []
        for perm_id in role.get('permissions', []):
            perm_data = source_permissions_by_id.get(perm_id)
            if perm_data:
                role_permissions_data.append(perm_data)
            else:
                log(f"Permission ID '{perm_id}' not found in source permissions.")
        role['permissionsData'] = role_permissions_data
        unique_roles[scope_key] = role

    unique_roles_list = list(unique_roles.values())
    if unique_roles_list:
        # Separate the unique roles into those with and without tenantId
        unique_roles_with_tenant = [role for role in unique_roles_list if role.get('tenantId')]
        unique_roles_without_tenant = [role for role in unique_roles_list if not role.get('tenantId')]
        # Create only the new (filtered) roles in the destination
        role_id_mapping = create_roles(destination_client, unique_roles_with_tenant, unique_roles_without_tenant)
        assign_permissions_to_roles(destination_client, unique_roles_list, role_id_mapping, dest_permissions_by_key)
    else:
        log("No new roles to create after filtering out existing roles.")

    log("Roles migration completed.")
