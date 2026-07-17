import requests
import json
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection
import os
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv()

# Migration flag
MIGRATE_PREHOOKS = os.getenv("MIGRATE_PREHOOKS", "False").lower() == "true"

def get_webhooks(client):
    """Fetches all webhook configurations from the account."""
    logger = get_logger()
    url = f"{client.base_url}/prehooks/resources/configurations/v1"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'frontegg-environment-id': client.client_id
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            webhooks = response.json()
            logger.info(f"  Found {len(webhooks)} webhook(s)")
            return webhooks
        else:
            logger.error(f"  Failed to get webhooks: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        logger.error(f"  Error getting webhooks: {e}")
        return []

def get_custom_code(client, code_id):
    """Fetches custom code content for a CUSTOM_CODE webhook."""
    logger = get_logger()
    url = f"{client.base_url}/custom-code/resources/codes/v1/{code_id}"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'frontegg-environment-id': client.client_id
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            code_data = response.json()
            # The API returns the code in 'content' field, not 'code'
            code = code_data.get('content', '')
            runtime = code_data.get('runtime', 'NODE_20')
            if code:
                logger.debug(f"    Retrieved custom code ({len(code)} chars), runtime: {runtime}")
            return code, runtime
        else:
            logger.error(f"    Failed to get custom code {code_id}: Status {response.status_code}")
            if response.text:
                logger.error(f"    Response: {response.text}")
            return None, None
    except Exception as e:
        logger.error(f"    Error getting custom code: {e}")
        return None, None

def create_custom_code_webhook(client, webhook_data, code, runtime):
    """Creates a CUSTOM_CODE type webhook in the destination."""
    logger = get_logger()
    url = f"{client.base_url}/prehooks/resources/configurations/v1/custom-code"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'frontegg-environment-id': client.client_id,
        'Content-Type': 'application/json'
    }
    
    # Prepare the webhook data
    event_keys = webhook_data.get("eventKeys", [])
    data = {
        "type": "CUSTOM_CODE",
        "id": "create",
        "eventKeys": event_keys,
        "eventKey": event_keys[0] if event_keys else "",  # First event key or empty string
        "displayName": webhook_data.get("displayName", ""),
        "isActive": webhook_data.get("isActive", False),
        "failMethod": webhook_data.get("failMethod", "OPEN"),
        "timeout": webhook_data.get("timeout", 10),
        "code": code,
        "runtime": runtime
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            logger.debug(f"  ✓ Created custom code webhook: {webhook_data.get('displayName')}")
            return True
        else:
            logger.error(f"  ✗ Failed to create custom code webhook: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"  ✗ Error creating custom code webhook: {e}")
        return False

def create_api_webhook(client, webhook_data):
    """Creates an API type webhook in the destination."""
    logger = get_logger()
    url = f"{client.base_url}/prehooks/resources/configurations/v1/api"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'frontegg-environment-id': client.client_id,
        'Content-Type': 'application/json'
    }
    
    # Prepare the webhook data
    event_keys = webhook_data.get("eventKeys", [])
    data = {
        "type": "API",
        "id": "create",
        "eventKeys": event_keys,
        "eventKey": event_keys[0] if event_keys else "",  # First event key or empty string
        "displayName": webhook_data.get("displayName", ""),
        "isActive": webhook_data.get("isActive", False),
        "failMethod": webhook_data.get("failMethod", "OPEN"),
        "timeout": webhook_data.get("timeout", 10),
        "url": webhook_data.get("url", ""),
        "secret": webhook_data.get("secret", "")
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            logger.debug(f"  ✓ Created API webhook: {webhook_data.get('displayName')}")
            return True
        else:
            logger.error(f"  ✗ Failed to create API webhook: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"  ✗ Error creating API webhook: {e}")
        return False

def delete_webhook(client, webhook_id):
    """Deletes a webhook by ID."""
    logger = get_logger()
    url = f"{client.base_url}/prehooks/resources/configurations/v1/{webhook_id}"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'frontegg-environment-id': client.client_id
    }
    
    try:
        response = requests.delete(url, headers=headers)
        if response.status_code in [200, 204]:
            return True
        else:
            logger.debug(f"  Failed to delete webhook {webhook_id}: {response.status_code}")
            return False
    except Exception as e:
        logger.debug(f"  Error deleting webhook: {e}")
        return False

def migrate_webhooks(source_client, destination_client):
    """Migrates webhooks from source to destination."""
    logger = get_logger()
    
    if not MIGRATE_PREHOOKS:
        log_warning("Prehooks migration is disabled (MIGRATE_PREHOOKS=False)")
        return
    
    log_subsection("Migrating Webhooks (Prehooks)")
    
    # Get source webhooks
    logger.info("🪝 Fetching webhooks from source account...")
    source_webhooks = get_webhooks(source_client)
    
    if not source_webhooks:
        log_warning("No webhooks found in source account")
        return
    
    # Get destination webhooks to check for conflicts
    logger.info("🪝 Fetching webhooks from destination account...")
    dest_webhooks = get_webhooks(destination_client)
    
    # Delete existing webhooks in destination if any
    if dest_webhooks:
        logger.info(f"🗑️  Deleting {len(dest_webhooks)} existing webhook(s) in destination...")
        for webhook in dest_webhooks:
            delete_webhook(destination_client, webhook['id'])
        time.sleep(1)  # Give the API a moment
    
    # Migrate webhooks
    logger.info(f"📥 Migrating {len(source_webhooks)} webhook(s)...")
    success_count = 0
    
    logger.start_progress(len(source_webhooks), "Migrating webhooks")
    
    for webhook in source_webhooks:
        webhook_name = webhook.get('displayName', 'Unknown')
        webhook_type = webhook.get('type', 'Unknown')
        logger.update_progress(description=f"Migrating {webhook_name}")
        
        if webhook_type == "CUSTOM_CODE":
            # Get the custom code content
            executor_id = webhook.get('executorIdentifier')
            if executor_id:
                code, runtime = get_custom_code(source_client, executor_id)
                if code:
                    if create_custom_code_webhook(destination_client, webhook, code, runtime):
                        success_count += 1
                else:
                    logger.error(f"  ✗ Could not retrieve code for {webhook_name}")
            else:
                logger.error(f"  ✗ No executor ID for custom code webhook {webhook_name}")
                
        elif webhook_type == "API":
            if create_api_webhook(destination_client, webhook):
                success_count += 1
        else:
            logger.warning(f"  ⚠ Unknown webhook type: {webhook_type}")
    
    logger.stop_progress()
    
    if success_count > 0:
        log_success(f"✓ Successfully migrated {success_count}/{len(source_webhooks)} webhooks")
    else:
        log_error(f"✗ Failed to migrate webhooks")

def migrate_webhook_configuration(source_client, destination_client):
    """Main function to migrate webhooks."""
    logger = get_logger()
    logger.section("Webhook Migration")
    
    migrate_webhooks(source_client, destination_client)