import json
import time
from utility.logger import get_logger, log_success, log_error, log_warning, log_subsection

# Security rule endpoints
SECURITY_RULES = {
    'bot-detection': 'Bot Detection',
    'device-fingerprint': 'Device Fingerprint',
    'brute-force': 'Brute Force Protection',
    'breached-password': 'Breached Password',
    'impossible-travel': 'Impossible Travel',
    'suspicious-ip': 'Suspicious IPs',
    'stale-users': 'Stale Users',
    'email-reputation': 'Email Credibility Check'
}

# Rate limit configuration
DEFAULT_RATE_LIMIT = 30
last_request_times = {}

def get_headers(client):
    """Generate headers with the client token."""
    return {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json"
    }

def enforce_rate_limit(endpoint, rate_limit=DEFAULT_RATE_LIMIT):
    """Enforce the rate limit for a specific endpoint."""
    interval = 60 / rate_limit
    
    if endpoint in last_request_times:
        time_since_last_request = time.time() - last_request_times[endpoint]
        if time_since_last_request < interval:
            time.sleep(interval - time_since_last_request)
    
    last_request_times[endpoint] = time.time()

def make_request_with_rate_limiting(method, url, client, headers=None, json_data=None):
    """Handle rate-limited requests."""
    enforce_rate_limit(url)
    try:
        response = client.session.request(method, url, headers=headers, json=json_data)
        if response.status_code == 429:
            log_warning("⚠ Rate limit exceeded. Retrying after delay...")
            time.sleep(60)
            response = client.session.request(method, url, headers=headers, json=json_data)
        response.raise_for_status()
        return response
    except Exception as e:
        log_error(f"Request failed: {e}")
        raise

def get_security_rule(client, rule_type):
    """Fetch a specific security rule configuration."""
    logger = get_logger()
    rule_name = SECURITY_RULES.get(rule_type, rule_type)
    logger.debug(f"Fetching {rule_name} configuration")
    
    endpoint = f"{client.base_url}/security-engines/resources/policies/v1/{rule_type}"
    headers = get_headers(client)
    
    try:
        response = make_request_with_rate_limiting('GET', endpoint, client, headers=headers)
        data = response.json()
        logger.debug(f"Retrieved {rule_name}: {data}")
        return data
    except Exception as e:
        log_warning(f"Failed to fetch {rule_name}: {e}")
        return None

def update_security_rule(client, rule_type, rule_config):
    """Update a security rule configuration."""
    logger = get_logger()
    rule_name = SECURITY_RULES.get(rule_type, rule_type)
    logger.debug(f"Updating {rule_name} configuration")
    
    endpoint = f"{client.base_url}/security-engines/resources/policies/v1/{rule_type}"
    headers = get_headers(client)
    
    try:
        response = make_request_with_rate_limiting('POST', endpoint, client, headers=headers, json_data=rule_config)
        log_success(f"✓ Updated {rule_name}")
        
        # Check if response has content before trying to parse JSON
        if response.text:
            try:
                return response.json()
            except json.JSONDecodeError:
                # Response might be empty or not JSON, which is fine for a POST
                return {'success': True}
        else:
            # Empty response is OK for update operations
            return {'success': True}
    except Exception as e:
        log_error(f"✗ Failed to update {rule_name}: {e}")
        return None

def compare_rules(source_rule, dest_rule):
    """Compare two security rule configurations."""
    if not source_rule or not dest_rule:
        return False
    
    # Compare the important fields that can be modified
    important_fields = ['action', 'enabled', 'threshold', 'timeWindow', 'lockDuration', 'challengeType']
    
    for field in important_fields:
        if field in source_rule or field in dest_rule:
            if source_rule.get(field) != dest_rule.get(field):
                return False
    
    # If all important fields match, consider them equal
    return True

def migrate_security_rules(source_client, destination_client):
    """Migrate all security rules from source to destination."""
    logger = get_logger()
    logger.info("🔐 Starting security rules migration process")
    
    # Track migration results
    migration_results = {
        'fetched': 0,
        'updated': 0,
        'skipped': 0,
        'failed': 0
    }
    
    log_subsection("Fetching Security Rules from Source")
    source_rules = {}
    
    # Fetch all security rules from source
    progress, task = logger.start_progress(len(SECURITY_RULES), "Fetching source security rules")
    
    for rule_type, rule_name in SECURITY_RULES.items():
        logger.update_progress(1, f"Fetching: {rule_name}")
        rule_config = get_security_rule(source_client, rule_type)
        if rule_config:
            source_rules[rule_type] = rule_config
            migration_results['fetched'] += 1
        else:
            log_warning(f"⚠ Could not fetch {rule_name} from source")
    
    logger.stop_progress()
    
    if not source_rules:
        log_error("No security rules found in source account")
        return
    
    log_success(f"Retrieved {len(source_rules)} security rules from source")
    
    # Compare and update destination rules
    log_subsection("Comparing and Updating Destination Rules")
    
    progress, task = logger.start_progress(len(source_rules), "Updating destination security rules")
    
    for rule_type, source_config in source_rules.items():
        rule_name = SECURITY_RULES.get(rule_type, rule_type)
        logger.update_progress(1, f"Processing: {rule_name}")
        
        # Get current destination configuration
        dest_config = get_security_rule(destination_client, rule_type)
        
        # Compare configurations
        if compare_rules(source_config, dest_config):
            logger.debug(f"{rule_name} is already up to date")
            migration_results['skipped'] += 1
        else:
            # Update destination with source configuration
            logger.info(f"📝 Updating {rule_name}")
            result = update_security_rule(destination_client, rule_type, source_config)
            
            if result:
                migration_results['updated'] += 1
                
                # Log the specific changes made
                if dest_config and source_config:
                    if isinstance(source_config, dict) and isinstance(dest_config, dict):
                        # Check for common fields that might have changed
                        if 'enabled' in source_config and 'enabled' in dest_config:
                            if source_config['enabled'] != dest_config.get('enabled'):
                                status = "Enabled" if source_config['enabled'] else "Disabled"
                                logger.info(f"  → {status} {rule_name}")
                        
                        if 'action' in source_config and source_config.get('action') != dest_config.get('action'):
                            logger.info(f"  → Action changed to: {source_config.get('action')}")
                        
                        if 'threshold' in source_config and source_config.get('threshold') != dest_config.get('threshold'):
                            logger.info(f"  → Threshold changed to: {source_config.get('threshold')}")
            else:
                migration_results['failed'] += 1
    
    logger.stop_progress()
    
    # Display final summary
    logger.print_stats("Security Rules Migration Summary", {
        "Total Rules Fetched": migration_results['fetched'],
        "Rules Updated": migration_results['updated'],
        "Rules Already Up-to-date": migration_results['skipped'],
        "Failed Updates": migration_results['failed']
    })
    
    # Show which rules were updated
    if migration_results['updated'] > 0:
        updated_rules = []
        for rule_type in source_rules:
            dest_config = get_security_rule(destination_client, rule_type)
            if not compare_rules(source_rules[rule_type], dest_config):
                updated_rules.append(SECURITY_RULES.get(rule_type, rule_type))
        
        if updated_rules:
            logger.print_summary(updated_rules, "Updated Security Rules")
    
    if migration_results['failed'] == 0:
        log_success("✅ Security rules migration completed successfully!")
    else:
        log_warning(f"⚠ Security rules migration completed with {migration_results['failed']} failures")