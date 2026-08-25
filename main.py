import os
from utility.frontegg_client import FronteggClient
from migration_scripts.tenants import migrate_tenants
from migration_scripts.permissions_and_categories import migrate_settings
from migration_scripts.roles import migrate_roles
from utility.logger import get_logger, log_section, log_success, log_error
from dotenv import load_dotenv
from migration_scripts.applications import migrate_applications
from migration_scripts.security_rules import migrate_security_rules
from migration_scripts.email_templates import migrate_email_configuration
from migration_scripts.webhooks import migrate_webhook_configuration
from migration_scripts.allowed_origins import migrate_allowed_origins_configuration
from migration_scripts.redirect_uris import migrate_redirect_uris_configuration
from migration_scripts.jwt_settings import migrate_jwt_settings
from migration_scripts.sso_configurations import migrate_sso_configurations
from migration_scripts.entitlements import migrate_entitlements
from migration_scripts.api_access_control import migrate_api_access_control
from migration_scripts.tenant_hierarchy import migrate_tenant_hierarchy
from migration_scripts.auth_policies import migrate_auth_policies
from migration_scripts.users_delta import migrate_missing_users
from migration_scripts.user_tenant_roles import migrate_user_tenant_roles
from migration_scripts.groups import migrate_groups
from migration_scripts.user_attributes import migrate_user_attributes
from migration_scripts.tenant_api_tokens import migrate_tenant_api_tokens
from migration_scripts.user_api_tokens import migrate_user_api_tokens

# Load environment variables
load_dotenv()

# Migration Steps Control - Read from environment variables
MIGRATE_TENANTS = os.getenv("MIGRATE_TENANTS", "False").lower() == "true"
MIGRATE_CATEGORIES = os.getenv("MIGRATE_CATEGORIES", "False").lower() == "true"
MIGRATE_PERMISSIONS = os.getenv("MIGRATE_PERMISSIONS", "False").lower() == "true"
MIGRATE_ROLES = os.getenv("MIGRATE_ROLES", "False").lower() == "true"
MIGRATE_APPLICATIONS = os.getenv("MIGRATE_APPLICATIONS", "False").lower() == "true"
MIGRATE_SECURITY_RULES = os.getenv("MIGRATE_SECURITY_RULES", "False").lower() == "true"
MIGRATE_EMAIL_TEMPLATES = os.getenv("MIGRATE_EMAIL_TEMPLATES", "False").lower() == "true"
MIGRATE_EMAIL_SENDER = os.getenv("MIGRATE_EMAIL_SENDER", "False").lower() == "true"
MIGRATE_PREHOOKS = os.getenv("MIGRATE_PREHOOKS", "False").lower() == "true"
MIGRATE_ALLOWED_ORIGINS = os.getenv("MIGRATE_ALLOWED_ORIGINS", "False").lower() == "true"
MIGRATE_REDIRECT_URIS = os.getenv("MIGRATE_REDIRECT_URIS", "False").lower() == "true"
MIGRATE_JWT_SETTINGS = os.getenv("MIGRATE_JWT_SETTINGS", "False").lower() == "true"
MIGRATE_SSO_CONFIGURATIONS = os.getenv("MIGRATE_SSO_CONFIGURATIONS", "False").lower() == "true"
MIGRATE_ENTITLEMENTS = os.getenv("MIGRATE_ENTITLEMENTS", "False").lower() == "true"
MIGRATE_API_ACCESS_CONTROL = os.getenv("MIGRATE_API_ACCESS_CONTROL", "False").lower() == "true"
MIGRATE_TENANT_HIERARCHY = os.getenv("MIGRATE_TENANT_HIERARCHY", "False").lower() == "true"
MIGRATE_AUTH_POLICIES = os.getenv("MIGRATE_AUTH_POLICIES", "False").lower() == "true"
MIGRATE_MISSING_USERS = os.getenv("MIGRATE_MISSING_USERS", "False").lower() == "true"
MIGRATE_GROUPS = os.getenv("MIGRATE_GROUPS", "False").lower() == "true"
MIGRATE_USER_ATTRIBUTES = os.getenv("MIGRATE_USER_ATTRIBUTES", "False").lower() == "true"
MIGRATE_USER_TENANT_ROLES = os.getenv("MIGRATE_USER_TENANT_ROLES", "False").lower() == "true"
MIGRATE_TENANT_API_TOKENS = os.getenv("MIGRATE_TENANT_API_TOKENS", "False").lower() == "true"
MIGRATE_USER_API_TOKENS = os.getenv("MIGRATE_USER_API_TOKENS", "False").lower() == "true"

def main():
    logger = get_logger()
    logger.section("Migration Process Starting")
    
    # Initialize Frontegg clients with authentication
    logger.subsection("Initializing Frontegg Clients")
    # The source account is opened read-only: its HTTP session refuses every
    # mutating request, so no step can ever write to the source environment.
    frontegg_client_1 = FronteggClient(os.getenv("BASE_URL_1"), os.getenv("CLIENT_ID_1"), os.getenv("API_KEY_1"), read_only=True)
    frontegg_client_2 = FronteggClient(os.getenv("BASE_URL_2"), os.getenv("CLIENT_ID_2"), os.getenv("API_KEY_2"))

    # Run migrations based on flags
    if frontegg_client_1.token and frontegg_client_2.token:
        migration_tasks = []
        if MIGRATE_TENANTS:
            migration_tasks.append("Tenants")
        if MIGRATE_CATEGORIES:
            migration_tasks.append("Categories")
        if MIGRATE_PERMISSIONS:
            migration_tasks.append("Permissions")
        if MIGRATE_ROLES:
            migration_tasks.append("Roles")
        if MIGRATE_APPLICATIONS:
            migration_tasks.append("Applications")
        if MIGRATE_SECURITY_RULES:
            migration_tasks.append("Security Rules")
        if MIGRATE_EMAIL_TEMPLATES:
            migration_tasks.append("Email Templates")
        if MIGRATE_EMAIL_SENDER:
            migration_tasks.append("Email Sender")
        if MIGRATE_PREHOOKS:
            migration_tasks.append("Prehooks")
        if MIGRATE_ALLOWED_ORIGINS:
            migration_tasks.append("Allowed Origins")
        if MIGRATE_REDIRECT_URIS:
            migration_tasks.append("Redirect URIs")
        if MIGRATE_JWT_SETTINGS:
            migration_tasks.append("JWT Settings")
        if MIGRATE_SSO_CONFIGURATIONS:
            migration_tasks.append("SSO Configurations")
        if MIGRATE_ENTITLEMENTS:
            migration_tasks.append("Entitlements")
        if MIGRATE_API_ACCESS_CONTROL:
            migration_tasks.append("API Access Control")
        if MIGRATE_TENANT_HIERARCHY:
            migration_tasks.append("Tenant Hierarchy")
        if MIGRATE_AUTH_POLICIES:
            migration_tasks.append("Auth Policies (MFA/lockout/password/captcha/sessions)")
        if MIGRATE_MISSING_USERS:
            migration_tasks.append("Missing Users (cutover delta)")
        if MIGRATE_USER_TENANT_ROLES:
            migration_tasks.append("User Tenant Memberships & Roles")
        if MIGRATE_GROUPS:
            migration_tasks.append("Groups")
        if MIGRATE_USER_ATTRIBUTES:
            migration_tasks.append("User Attributes (lock state, phone, metadata)")
        if MIGRATE_TENANT_API_TOKENS:
            migration_tasks.append("Tenant API Tokens")
        if MIGRATE_USER_API_TOKENS:
            migration_tasks.append("User API Tokens")

        logger.print_summary(migration_tasks, "Scheduled Migration Tasks")
        
        if MIGRATE_TENANTS:
            log_section("Tenant Migration")
            migrate_tenants(frontegg_client_1, frontegg_client_2)

        # Hierarchy links tenants that already exist, so it must follow tenants.
        if MIGRATE_TENANT_HIERARCHY:
            migrate_tenant_hierarchy(frontegg_client_1, frontegg_client_2)

        if MIGRATE_SSO_CONFIGURATIONS:
            log_section("SSO Configurations Migration")
            migrate_sso_configurations(frontegg_client_1, frontegg_client_2)

        if MIGRATE_ENTITLEMENTS:
            log_section("Entitlements Migration")
            migrate_entitlements(frontegg_client_1, frontegg_client_2)

        if MIGRATE_API_ACCESS_CONTROL:
            log_section("API Access Control Migration")
            migrate_api_access_control(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_CATEGORIES or MIGRATE_PERMISSIONS:
            log_section("Settings Migration (Categories & Permissions)")
            migrate_settings(frontegg_client_1, frontegg_client_2, MIGRATE_CATEGORIES, MIGRATE_PERMISSIONS)

        if MIGRATE_ROLES:
            log_section("Roles Migration")
            migrate_roles(frontegg_client_1, frontegg_client_2)

        if MIGRATE_APPLICATIONS:
            log_section("Applications Migration")
            migrate_applications(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_SECURITY_RULES:
            log_section("Security Rules Migration")
            migrate_security_rules(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_EMAIL_TEMPLATES or MIGRATE_EMAIL_SENDER:
            log_section("Email Configuration Migration")
            migrate_email_configuration(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_PREHOOKS:
            log_section("Prehooks Migration")
            migrate_webhook_configuration(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_ALLOWED_ORIGINS:
            log_section("Allowed Origins Migration")
            migrate_allowed_origins_configuration(frontegg_client_1, frontegg_client_2)

        if MIGRATE_REDIRECT_URIS:
            log_section("Redirect URIs Migration")
            migrate_redirect_uris_configuration(frontegg_client_1, frontegg_client_2)
        
        if MIGRATE_JWT_SETTINGS:
            migrate_jwt_settings(frontegg_client_1, frontegg_client_2)

        if MIGRATE_AUTH_POLICIES:
            log_section("Auth Policies Migration")
            migrate_auth_policies(frontegg_client_1, frontegg_client_2)

        # These depend on roles (for ID mapping) and on users already existing in
        # the destination, so they run last. Delta-user creation runs first so the
        # membership step can then fill in the rest of their tenant memberships.
        if MIGRATE_MISSING_USERS:
            migrate_missing_users(frontegg_client_1, frontegg_client_2)

        if MIGRATE_USER_TENANT_ROLES:
            migrate_user_tenant_roles(frontegg_client_1, frontegg_client_2)

        # Groups attach existing users, so they follow the membership migration.
        if MIGRATE_GROUPS:
            migrate_groups(frontegg_client_1, frontegg_client_2)

        if MIGRATE_USER_ATTRIBUTES:
            migrate_user_attributes(frontegg_client_1, frontegg_client_2)

        if MIGRATE_TENANT_API_TOKENS:
            migrate_tenant_api_tokens(frontegg_client_1, frontegg_client_2)

        # User tokens inherit the user's roles on the tenant, so this follows the
        # membership migration above.
        if MIGRATE_USER_API_TOKENS:
            migrate_user_api_tokens(frontegg_client_1, frontegg_client_2)

        log_success("🎉 Migration process completed successfully!")
        
    else:
        log_error("✗ Authentication failed; migration aborted.")

if __name__ == "__main__":
    main()
