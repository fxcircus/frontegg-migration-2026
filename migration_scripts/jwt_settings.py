import requests
import json
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Migration flag
MIGRATE_JWT_SETTINGS = os.getenv("MIGRATE_JWT_SETTINGS", "False").lower() == "true"

def get_jwt_settings(client):
    """Fetches JWT settings from the account."""
    logger = get_logger()
    url = f"{client.base_url}/identity/resources/configurations/v1"
    headers = {'Authorization': f'Bearer {client.token}'}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # Extract relevant JWT settings
            jwt_settings = {
                "defaultTokenExpiration": data.get("defaultTokenExpiration"),
                "defaultRefreshTokenExpiration": data.get("defaultRefreshTokenExpiration"),
                "cookieSameSite": data.get("cookieSameSite")
            }
            # Remove None values
            jwt_settings = {k: v for k, v in jwt_settings.items() if v is not None}
            return jwt_settings
        else:
            logger.error(f"Failed to get JWT settings: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error getting JWT settings: {e}")
        return None

def update_jwt_settings(client, settings):
    """Updates JWT settings in the destination account."""
    logger = get_logger()
    url = f"{client.base_url}/identity/resources/configurations/v1"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json',
        'frontegg-vendor-id': client.client_id
    }
    
    try:
        response = requests.post(url, headers=headers, json=settings)
        if response.status_code in [200, 201]:
            return True
        else:
            logger.error(f"Failed to update JWT settings: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error updating JWT settings: {e}")
        return False

def compare_jwt_settings(source_settings, dest_settings):
    """Compares JWT settings to check if they need updating."""
    if not source_settings or not dest_settings:
        return True
    
    for key in source_settings:
        if source_settings.get(key) != dest_settings.get(key):
            return True
    
    return False

def migrate_jwt_settings(source_client, destination_client):
    """Migrates JWT settings from source to destination."""
    logger = get_logger()
    
    if not MIGRATE_JWT_SETTINGS:
        log_warning("JWT settings migration is disabled (MIGRATE_JWT_SETTINGS=False)")
        return
    
    logger.section("JWT Settings Migration")
    log_subsection("Migrating JWT Settings")
    
    # Get JWT settings from source
    logger.info("🔐 Fetching JWT settings from source account...")
    source_settings = get_jwt_settings(source_client)
    
    if not source_settings:
        log_error("✗ Failed to fetch JWT settings from source account")
        return
    
    logger.info(f"  Found settings: {json.dumps(source_settings, indent=2)}")
    
    # Get JWT settings from destination
    logger.info("🔐 Fetching JWT settings from destination account...")
    dest_settings = get_jwt_settings(destination_client)
    
    # Compare settings
    if not compare_jwt_settings(source_settings, dest_settings):
        log_success("✓ JWT settings are already up to date in destination")
        return
    
    # Update settings in destination
    logger.info("🔄 Updating JWT settings in destination account...")
    if update_jwt_settings(destination_client, source_settings):
        log_success("✓ Successfully migrated JWT settings")
        logger.info(f"  Updated settings: {json.dumps(source_settings, indent=2)}")
    else:
        log_error("✗ Failed to migrate JWT settings")