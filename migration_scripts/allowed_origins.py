import requests
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Migration flag
MIGRATE_ALLOWED_ORIGINS = os.getenv("MIGRATE_ALLOWED_ORIGINS", "False").lower() == "true"

def get_vendor_details(client):
    """Fetches vendor details including allowed origins."""
    logger = get_logger()
    url = f"{client.base_url}/vendors"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'accept': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"  Failed to get vendor details: {response.status_code}")
            if response.text:
                logger.error(f"  Response: {response.text}")
            return None
    except Exception as e:
        logger.error(f"  Error getting vendor details: {e}")
        return None

def update_allowed_origins(client, allowed_origins):
    """Updates allowed origins for the vendor."""
    logger = get_logger()
    url = f"{client.base_url}/vendors"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json',
        'accept': 'application/json'
    }
    
    data = {
        "allowedOrigins": allowed_origins
    }
    
    try:
        response = requests.put(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            return True
        else:
            logger.error(f"  Failed to update allowed origins: {response.status_code}")
            if response.text:
                logger.error(f"  Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"  Error updating allowed origins: {e}")
        return False

def migrate_allowed_origins(source_client, destination_client):
    """Migrates allowed origins from source to destination."""
    logger = get_logger()
    
    if not MIGRATE_ALLOWED_ORIGINS:
        log_warning("Allowed origins migration is disabled (MIGRATE_ALLOWED_ORIGINS=False)")
        return
    
    log_subsection("Migrating Allowed Origins")
    
    # Get source vendor details
    logger.info("🔗 Fetching allowed origins from source account...")
    source_vendor = get_vendor_details(source_client)
    
    if not source_vendor:
        log_error("Failed to fetch source vendor details")
        return
    
    source_origins = source_vendor.get('allowedOrigins', [])
    if not source_origins:
        log_warning("No allowed origins found in source account")
        return
    
    logger.info(f"  Found {len(source_origins)} allowed origin(s)")
    for origin in source_origins[:5]:  # Show first 5
        logger.debug(f"    - {origin}")
    if len(source_origins) > 5:
        logger.debug(f"    ... and {len(source_origins) - 5} more")
    
    # Get destination vendor details
    logger.info("🔗 Fetching allowed origins from destination account...")
    dest_vendor = get_vendor_details(destination_client)
    
    if not dest_vendor:
        log_error("Failed to fetch destination vendor details")
        return
    
    dest_origins = dest_vendor.get('allowedOrigins', [])
    logger.info(f"  Found {len(dest_origins)} existing allowed origin(s)")
    
    # Merge origins (keep unique)
    merged_origins = list(set(source_origins + dest_origins))
    new_origins_count = len(merged_origins) - len(dest_origins)
    
    if new_origins_count == 0:
        log_success("✓ Allowed origins already up to date")
    else:
        # Update allowed origins
        logger.info(f"🔄 Adding {new_origins_count} new allowed origin(s)...")
        logger.info(f"   Total after merge: {len(merged_origins)} origins")
        
        if update_allowed_origins(destination_client, merged_origins):
            log_success(f"✓ Successfully updated allowed origins ({new_origins_count} new)")
        else:
            log_error("✗ Failed to update allowed origins")

def migrate_allowed_origins_configuration(source_client, destination_client):
    """Main function to migrate allowed origins."""
    logger = get_logger()
    logger.section("Allowed Origins Migration")
    
    migrate_allowed_origins(source_client, destination_client)