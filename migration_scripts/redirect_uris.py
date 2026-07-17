import os
import requests
from dotenv import load_dotenv
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection

# Load environment variables
load_dotenv()

# Migration flag
MIGRATE_REDIRECT_URIS = os.getenv("MIGRATE_REDIRECT_URIS", "False").lower() == "true"


def get_oauth_configuration(client):
    """Fetches OAuth configuration."""
    logger = get_logger()
    url = f"{client.base_url}/oauth/resources/configurations/v1"
    headers = {
        "Authorization": f"Bearer {client.token}",
        "accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else {}
        logger.error(f"  Failed to get OAuth configuration: {response.status_code}")
        if response.text:
            logger.error(f"  Response: {response.text}")
        return {}
    except Exception as e:
        logger.error(f"  Error getting OAuth configuration: {e}")
        return {}


def get_redirect_uris(client):
    """Fetches redirect URIs configuration."""
    logger = get_logger()
    url = f"{client.base_url}/oauth/resources/configurations/v1/redirect-uri"
    headers = {
        "Authorization": f"Bearer {client.token}",
        "accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and "redirectUris" in data:
                return data["redirectUris"]
            if isinstance(data, list):
                return data
            return []
        logger.error(f"  Failed to get redirect URIs: {response.status_code}")
        if response.text:
            logger.error(f"  Response: {response.text}")
        return []
    except Exception as e:
        logger.error(f"  Error getting redirect URIs: {e}")
        return []


def add_redirect_uri(client, redirect_uri):
    """Adds a single redirect URI."""
    logger = get_logger()
    url = f"{client.base_url}/oauth/resources/configurations/v1/redirect-uri"
    headers = {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }
    data = {"redirectUri": redirect_uri}

    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            return True
        logger.error(f"    Failed to add redirect URI {redirect_uri}: {response.status_code}")
        if response.text:
            logger.debug(f"    Response: {response.text}")
        return False
    except Exception as e:
        logger.error(f"    Error adding redirect URI: {e}")
        return False


def deactivate_oauth_configuration(client):
    """Deactivates OAuth configuration in destination."""
    logger = get_logger()
    url = f"{client.base_url}/oauth/resources/configurations/v1/deactivate"
    headers = {
        "Authorization": f"Bearer {client.token}",
        "accept": "application/json",
    }
    try:
        response = requests.post(url, headers=headers)
        if response.status_code in [200, 201, 204]:
            return True
        logger.error(f"  Failed to deactivate OAuth configuration: {response.status_code}")
        if response.text:
            logger.error(f"  Response: {response.text}")
        return False
    except Exception as e:
        logger.error(f"  Error deactivating OAuth configuration: {e}")
        return False


def migrate_redirect_uris(source_client, destination_client):
    """Migrates redirect URIs from source to destination."""
    logger = get_logger()

    if not MIGRATE_REDIRECT_URIS:
        log_warning("Redirect URIs migration is disabled (MIGRATE_REDIRECT_URIS=False)")
        return

    log_subsection("Migrating Redirect URIs")

    logger.info("🔐 Fetching OAuth configuration from source account...")
    source_oauth_config = get_oauth_configuration(source_client)
    source_is_active = bool(source_oauth_config.get("isActive"))
    if not source_is_active:
        logger.info("🔐 Source OAuth configuration is inactive. Deactivating destination OAuth configuration...")
        if deactivate_oauth_configuration(destination_client):
            log_success("✓ Destination OAuth configuration deactivated")
        else:
            log_error("✗ Failed to deactivate destination OAuth configuration")
        return

    logger.info("🔗 Fetching redirect URIs from source account...")
    source_uris = get_redirect_uris(source_client)
    if not source_uris:
        log_warning("No redirect URIs found in source account")
        return
    logger.info(f"  Found {len(source_uris)} redirect URI(s)")

    logger.info("🔗 Fetching redirect URIs from destination account...")
    dest_uris = get_redirect_uris(destination_client)
    logger.info(f"  Found {len(dest_uris)} existing redirect URI(s)")

    def normalize_uri(uri):
        if isinstance(uri, str):
            return uri
        if isinstance(uri, dict):
            return uri.get("redirectUri", uri.get("uri", str(uri)))
        return str(uri)

    source_normalized = [normalize_uri(uri) for uri in source_uris]
    dest_normalized = [normalize_uri(uri) for uri in dest_uris]
    missing_uris = [uri for uri in source_normalized if uri not in dest_normalized]

    if not missing_uris:
        log_success("✓ Redirect URIs already up to date")
        return

    logger.info(f"🔄 Adding {len(missing_uris)} missing redirect URI(s)...")
    success_count = 0

    logger.start_progress(len(missing_uris), "Adding redirect URIs")
    for uri in missing_uris:
        display_uri = uri[:50] + "..." if len(uri) > 50 else uri
        logger.update_progress(description=f"Adding: {display_uri}")
        if add_redirect_uri(destination_client, uri):
            success_count += 1
            logger.debug(f"  ✓ Added: {uri}")
        else:
            logger.debug(f"  ✗ Failed: {uri}")
    logger.stop_progress()

    if success_count == len(missing_uris):
        log_success(f"✓ Successfully added all {success_count} redirect URI(s)")
    elif success_count > 0:
        log_warning(f"⚠ Added {success_count}/{len(missing_uris)} redirect URI(s)")
    else:
        log_error("✗ Failed to add redirect URIs")


def migrate_redirect_uris_configuration(source_client, destination_client):
    logger = get_logger()
    logger.section("Redirect URIs Migration")
    migrate_redirect_uris(source_client, destination_client)
