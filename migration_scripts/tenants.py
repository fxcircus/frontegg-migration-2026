import json
import time
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection

# Rate limit configuration
DEFAULT_RATE_LIMIT = 30  # Default to 30 requests per minute
RATE_LIMITS = {
    # Custom rate limits for specific endpoints can be specified here
    # 'https://api.frontegg.com/tenants/resources/tenants/v1': 30,
}

# Track the timestamps of requests for each endpoint
last_request_times = {}

def get_headers(client):
    """Generate headers with the client token."""
    return {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json"
    }

def get_rate_limit(endpoint):
    """Get the rate limit for the endpoint, or use the default."""
    return RATE_LIMITS.get(endpoint, DEFAULT_RATE_LIMIT)

def enforce_rate_limit(endpoint):
    """Enforce the rate limit for a specific endpoint."""
    rate_limit = get_rate_limit(endpoint)
    interval = 60 / rate_limit  # Time in seconds between requests

    if endpoint in last_request_times:
        time_since_last_request = time.time() - last_request_times[endpoint]
        if time_since_last_request < interval:
            # Wait for the required interval to avoid exceeding the rate limit
            time.sleep(interval - time_since_last_request)

    # Update the last request time for this endpoint
    last_request_times[endpoint] = time.time()

def _refresh_auth_header(client, headers):
    """Re-authenticate if the client's token expired and sync the Authorization header.

    Long migrations outlive the initial vendor token; callers build header dicts
    once and reuse them across loops, so the refreshed token must be written back
    into the same dict. Clients without ensure_token (e.g. test doubles) are left alone.
    """
    ensure = getattr(client, "ensure_token", None)
    if not callable(ensure):
        return
    token = ensure()
    if headers:
        for key in headers:
            if key.lower() == "authorization":
                headers[key] = f"Bearer {token}"

def make_request_with_rate_limiting(method, url, client, headers=None, params=None, json_data=None):
    """Handle rate-limited requests."""
    enforce_rate_limit(url)
    _refresh_auth_header(client, headers)
    try:
        response = client.session.request(method, url, headers=headers, params=params, json=json_data)
        if response.status_code == 429:
            log_warning("⚠ Rate limit exceeded. Retrying after delay...")
            time.sleep(60)  # Wait 60 seconds when hitting rate limits (adjust as needed)
            response = client.session.request(method, url, headers=headers, params=params, json=json_data)
        response.raise_for_status()
        return response
    except Exception as e:
        log_error(f"Request failed: {e}")
        raise

def get_tenants(client):
    logger = get_logger()
    logger.debug("Fetching tenants from source")
    headers = get_headers(client)
    tenants = []
    endpoint = client.base_url + '/tenants/resources/tenants/v2'
    page_limit = 200
    page_index = 0
    total_pages = None

    while True:
        params = {
            "_limit": page_limit,
            "_offset": page_index,
        }
        try:
            response = make_request_with_rate_limiting(
                'GET',
                endpoint,
                client,
                headers=headers,
                params=params,
            )
            response_json = response.json()
        except Exception as e:
            log_error(f"Error fetching tenants: {e}")
            if tenants:
                log_warning(
                    f"Continuing with partial tenant list ({len(tenants)} tenants) due to pagination error."
                )
            return tenants

        items = response_json.get('items', []) if isinstance(response_json, dict) else []
        metadata = response_json.get('_metadata', {}) if isinstance(response_json, dict) else {}
        page_total = metadata.get('totalPages')
        if isinstance(page_total, int) and page_total > 0:
            total_pages = page_total
        tenants.extend(items)

        if total_pages is not None and page_index >= total_pages - 1:
            break
        if not items:
            break
        page_index += 1

    log_success(f"Retrieved {len(tenants)} tenants")
    return tenants

def create_tenant(client, tenant):
    logger = get_logger()
    logger.debug(f"Creating tenant: {tenant['tenantId']} - {tenant['name']}")
    endpoint = client.base_url + '/tenants/resources/tenants/v1'
    headers = get_headers(client)
    req_body = {
        'tenantId': tenant['tenantId'],
        'name': tenant['name'],
    }
    try:
        response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=req_body)
        log_success(f"✓ Created tenant: {tenant['tenantId']}")
        return response
    except Exception as e:
        log_error(f"✗ Failed to create tenant {tenant['tenantId']}: {e}")

def set_tenant_metadata(client, tenant_id, metadata):
    logger = get_logger()
    logger.debug(f"Setting metadata for tenant {tenant_id}")
    endpoint = client.base_url + f'/tenants/resources/tenants/v1/{tenant_id}/metadata'
    headers = get_headers(client)
    try:
        response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data={'metadata': metadata})
        log_success(f'✓ Updated metadata for tenant {tenant_id}')
        return response
    except Exception as e:
        log_error(f"✗ Failed to update metadata for tenant {tenant_id}: {e}")

def bulk_create_tenants(destination_client, tenants):
    logger = get_logger()
    log_subsection("Bulk Tenant Creation")
    
    existing_tenants = {t['tenantId'] for t in get_tenants(destination_client)}
    
    new_tenants = [t for t in tenants if t['tenantId'] not in existing_tenants]
    skipped_tenants = [t for t in tenants if t['tenantId'] in existing_tenants]
    
    if skipped_tenants:
        log_warning(f"⚠ Skipping {len(skipped_tenants)} existing tenants")
    
    if new_tenants:
        progress, task = logger.start_progress(len(new_tenants), "Creating tenants")
        for tenant in new_tenants:
            create_tenant(destination_client, tenant)
            logger.update_progress(1, f"Creating: {tenant['tenantId']}")
        logger.stop_progress()
    
    logger.print_stats("Tenant Creation Summary", {
        "Total Tenants": len(tenants),
        "Created": len(new_tenants),
        "Skipped (Existing)": len(skipped_tenants)
    })

def migrate_tenants(source_client, destination_client):
    logger = get_logger()
    logger.info("🚀 Starting tenant migration process")
    
    # Fetch tenants from source
    source_tenants = get_tenants(source_client)
    if not source_tenants:
        log_warning("No tenants found to migrate")
        return
    
    # Create tenants in destination
    bulk_create_tenants(destination_client, source_tenants)
    
    # Migrate metadata
    tenants_with_metadata = [t for t in source_tenants if t.get('metadata')]
    if tenants_with_metadata:
        log_subsection("Migrating Tenant Metadata")
        progress, task = logger.start_progress(len(tenants_with_metadata), "Updating metadata")
        
        success_count = 0
        for tenant in tenants_with_metadata:
            try:
                metadata_json = json.loads(tenant['metadata'])
                set_tenant_metadata(destination_client, tenant['tenantId'], metadata_json)
                success_count += 1
            except json.JSONDecodeError:
                log_warning(f"⚠ Invalid metadata for tenant {tenant['tenantId']}")
            logger.update_progress(1)
        
        logger.stop_progress()
        logger.print_stats("Metadata Migration Summary", {
            "Total with Metadata": len(tenants_with_metadata),
            "Successfully Updated": success_count,
            "Failed": len(tenants_with_metadata) - success_count
        })
    
    log_success("✅ Tenant migration completed successfully!")
