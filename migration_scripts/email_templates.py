import requests
import json
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Migration flags
MIGRATE_EMAIL_TEMPLATES = os.getenv("MIGRATE_EMAIL_TEMPLATES", "False").lower() == "true"
MIGRATE_EMAIL_SENDER = os.getenv("MIGRATE_EMAIL_SENDER", "False").lower() == "true"

# Email template types to migrate
EMAIL_TEMPLATE_TYPES = [
    "ActivateUser",
    "ResetPassword",
    "MagicLink",
    "MagicCode",
    "ConnectOtpAuthenticator",
    "EnrollMfaAuthenticator",
    "UserInvitation",
    "PwlessInvitation",
    "ResetPhoneNumber",
    "VerifyEmail",
    "VerifyPhoneNumber",
    "ResetMfa",
    "RemoveUser",
    "BulkInviteTemplate",
    "NewDeviceConnected",
    "UserUsedInvitation",
    "EmailVerification"
]

def get_email_templates(client):
    """Fetches all email templates from the account."""
    logger = get_logger()
    
    # First try to get all templates at once
    url = f"{client.base_url}/identity/resources/mail/v1/configs/templates"
    headers = {'Authorization': f'Bearer {client.token}'}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            all_templates = response.json()
            if isinstance(all_templates, list):
                # Convert list to dict by template type
                templates = {}
                for template in all_templates:
                    template_type = template.get("type")
                    if template_type:
                        templates[template_type] = template
                        logger.debug(f"  ✓ Retrieved {template_type} template")
                return templates
    except Exception as e:
        logger.debug(f"  Bulk fetch failed, trying individual templates: {e}")
    
    # Fall back to individual template fetching
    templates = {}
    for template_type in EMAIL_TEMPLATE_TYPES:
        try:
            response = requests.get(f"{url}/{template_type}", headers=headers)
            if response.status_code == 200:
                template_data = response.json()
                templates[template_type] = template_data
                logger.debug(f"  ✓ Retrieved {template_type} template")
            elif response.status_code == 404:
                logger.debug(f"  ⚠ Template {template_type} not found")
            else:
                logger.debug(f"  ✗ Failed to get {template_type}: {response.status_code}")
        except Exception as e:
            logger.debug(f"  ✗ Error getting {template_type}: {e}")
    
    return templates

def compare_templates(source_template, dest_template):
    """Compares two templates to check if they need updating."""
    # Fields to compare (excluding URLs that should be preserved in destination)
    fields_to_compare = [
        'htmlTemplate',
        'subject',
        'fromName',
        'active',
        'senderEmail'
    ]
    
    for field in fields_to_compare:
        source_val = source_template.get(field)
        dest_val = dest_template.get(field)
        if source_val != dest_val:
            return True
    
    return False

def update_email_template(client, template_type, template_data, dest_urls):
    """Updates an email template in the destination account."""
    logger = get_logger()
    url = f"{client.base_url}/identity/resources/mail/v1/configs/templates"
    headers = {
        'Authorization': f'Bearer {client.token}',
        'Content-Type': 'application/json',
        'frontegg-vendor-id': client.client_id  # Add vendor ID header
    }
    
    # Prepare the update payload
    # We preserve destination URLs but update the content
    update_data = {
        "type": template_type,
        "htmlTemplate": template_data.get("htmlTemplate", ""),
        "subject": template_data.get("subject", ""),
        "fromName": template_data.get("fromName", ""),
        "active": template_data.get("active", True),
        "senderEmail": template_data.get("senderEmail", "")
    }
    
    # Preserve destination URLs
    if dest_urls:
        if "redirectURL" in dest_urls:
            update_data["redirectURL"] = dest_urls["redirectURL"]
        if "successRedirectUrl" in dest_urls:
            update_data["successRedirectUrl"] = dest_urls["successRedirectUrl"]
        if "redirectURLPattern" in dest_urls:
            update_data["redirectURLPattern"] = dest_urls["redirectURLPattern"]
        if "successRedirectUrlPattern" in dest_urls:
            update_data["successRedirectUrlPattern"] = dest_urls["successRedirectUrlPattern"]
    
    try:
        response = requests.post(url, headers=headers, json=update_data)
        if response.status_code in [200, 201]:
            return True
        else:
            logger.error(f"    ✗ Failed to update {template_type}: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"    ✗ Error updating {template_type}: {e}")
        return False

def migrate_email_templates(source_client, destination_client):
    """Migrates email templates from source to destination."""
    logger = get_logger()
    
    if not MIGRATE_EMAIL_TEMPLATES:
        log_warning("Email templates migration is disabled (MIGRATE_EMAIL_TEMPLATES=False)")
        return
    
    log_subsection("Migrating Email Templates")
    
    # Get templates from both accounts
    logger.info("📧 Fetching email templates from source account...")
    source_templates = get_email_templates(source_client)
    
    logger.info("📧 Fetching email templates from destination account...")
    dest_templates = get_email_templates(destination_client)
    
    if not source_templates:
        log_warning("No email templates found in source account")
        return
    
    # Compare and migrate templates
    templates_to_update = []
    templates_unchanged = []
    
    for template_type, source_template in source_templates.items():
        dest_template = dest_templates.get(template_type)
        
        if dest_template:
            # Check if update is needed
            if compare_templates(source_template, dest_template):
                templates_to_update.append(template_type)
            else:
                templates_unchanged.append(template_type)
        else:
            # New template to create
            templates_to_update.append(template_type)
    
    logger.info(f"📊 Templates to update: {len(templates_to_update)}")
    logger.info(f"📊 Templates unchanged: {len(templates_unchanged)}")
    
    if templates_to_update:
        logger.info("🔄 Updating email templates...")
        success_count = 0
        
        logger.start_progress(len(templates_to_update), "Migrating templates")
        for template_type in templates_to_update:
            logger.update_progress(description=f"Updating {template_type}")
            source_template = source_templates[template_type]
            dest_template = dest_templates.get(template_type, {})
            
            # Preserve destination URLs
            dest_urls = {
                "redirectURL": dest_template.get("redirectURL"),
                "successRedirectUrl": dest_template.get("successRedirectUrl"),
                "redirectURLPattern": dest_template.get("redirectURLPattern"),
                "successRedirectUrlPattern": dest_template.get("successRedirectUrlPattern")
            }
            
            if update_email_template(destination_client, template_type, source_template, dest_urls):
                success_count += 1
                logger.debug(f"  ✓ Updated {template_type}")
            else:
                logger.debug(f"  ✗ Failed to update {template_type}")
        
        logger.stop_progress()
        log_success(f"✓ Successfully migrated {success_count}/{len(templates_to_update)} email templates")
    else:
        log_success("✓ All email templates are already up to date")

def get_email_provider(client):
    """Gets the email provider configuration."""
    logger = get_logger()
    url = f"{client.base_url}/identity/resources/mail/v1/configurations"
    headers = {'Authorization': f'Bearer {client.token}'}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, dict):
                return data
            return None
        return None
    except Exception as e:
        logger.debug(f"  ✗ Error getting email provider: {e}")
        return None

def migrate_email_provider(source_client, destination_client):
    """Migrates email provider configuration from source to destination."""
    logger = get_logger()
    
    if not MIGRATE_EMAIL_SENDER:
        log_warning("Email sender migration is disabled (MIGRATE_EMAIL_SENDER=False)")
        return
    
    log_subsection("Migrating Email Provider Configuration")
    
    # Get source email provider
    logger.info("📮 Fetching email provider from source account...")
    source_provider = get_email_provider(source_client)
    
    if not source_provider:
        log_warning("No email provider configured in source account")
        return
    
    provider_type = source_provider.get("provider")
    secret = source_provider.get("secret")
    
    if not provider_type or not secret:
        log_warning("Invalid provider configuration in source account")
        return
    
    logger.info(f"📮 Found {provider_type} provider in source account")
    
    # Check destination provider
    dest_provider = get_email_provider(destination_client)
    if dest_provider:
        if dest_provider.get("provider") == provider_type and dest_provider.get("secret") == secret:
            log_success("✓ Email provider already configured correctly in destination")
            return
    
    # Configure provider in destination
    logger.info(f"🔄 Configuring {provider_type} provider in destination account...")
    
    # Try v1 endpoint first (which worked in the past)
    url = f"{destination_client.base_url}/identity/resources/mail/v1/configurations"
    headers = {
        'Authorization': f'Bearer {destination_client.token}',
        'Content-Type': 'application/json',
        'frontegg-vendor-id': destination_client.client_id  # Add vendor ID header
    }
    
    data = {
        "provider": provider_type,
        "secret": secret
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code in [200, 201]:
            log_success(f"✓ Successfully configured {provider_type} email provider")
        elif response.status_code == 403 or response.status_code == 404:
            # Try v2 endpoint as fallback
            logger.debug(f"  v1 endpoint failed with {response.status_code}, trying v2...")
            url = f"{destination_client.base_url}/identity/resources/mail/v2/configurations"
            response = requests.post(url, headers=headers, json=data)
            if response.status_code in [200, 201]:
                log_success(f"✓ Successfully configured {provider_type} email provider")
            else:
                log_error(f"✗ Failed to configure email provider: {response.status_code} - {response.text}")
        else:
            log_error(f"✗ Failed to configure email provider: {response.status_code} - {response.text}")
    except Exception as e:
        log_error(f"✗ Error configuring email provider: {e}")

def migrate_email_configuration(source_client, destination_client):
    """Main function to migrate email templates and provider."""
    logger = get_logger()
    logger.section("Email Configuration Migration")
    
    if MIGRATE_EMAIL_SENDER:
        migrate_email_provider(source_client, destination_client)

    if MIGRATE_EMAIL_TEMPLATES:
        migrate_email_templates(source_client, destination_client)
    
    if not MIGRATE_EMAIL_TEMPLATES and not MIGRATE_EMAIL_SENDER:
        log_warning("Both email templates and sender migration are disabled")