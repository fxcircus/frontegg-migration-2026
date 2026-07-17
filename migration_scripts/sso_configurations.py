import requests

from migration_scripts.tenants import get_tenants
from migration_scripts.roles import get_roles
from utility.logger import get_logger, log_error, log_success, log_warning, log_subsection


SSO_ENDPOINT = "/team/resources/sso/v1/configurations"
SSO_METADATA_ENDPOINT = "/team/resources/sso/v1/configurations/metadata"
SSO_ROLES_ENDPOINT = "/team/resources/sso/v1/configurations/{configuration_id}/roles"
SSO_GROUPS_ENDPOINT = "/team/resources/sso/v1/configurations/{configuration_id}/groups"
SSO_DOMAINS_ENDPOINT = "/team/resources/sso/v1/configurations/{configuration_id}/domains"
SSO_DOMAINS_CONFIG_ENDPOINT = "/team/resources/sso/v1/configurations/domains"
SSO_MULTIPLE_PER_DOMAIN_ENDPOINT = "/team/resources/sso/v1/configurations/multiple-sso-per-domain"

# Keep only creation-relevant fields from source responses.
CREATE_SSO_FIELDS = [
    "enabled",
    "ssoEndpoint",
    "publicCertificate",
    "signRequest",
    "acsUrl",
    "spEntityId",
    "type",
    "oidcClientId",
    "oidcSecret",
    "configMetadata",
    "overrideActiveTenant",
    "subAccountAccessLimit",
    "idpClientId",
    "idpClientSecret",
]


def _headers(client, tenant_id=None):
    headers = {
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json",
    }
    if tenant_id:
        headers["frontegg-tenant-id"] = tenant_id
    return headers


def _extract_items(response_json):
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        if isinstance(response_json.get("items"), list):
            return response_json["items"]
        if isinstance(response_json.get("configurations"), list):
            return response_json["configurations"]
    return []


def _sso_signature(config):
    return (
        config.get("type"),
        config.get("ssoEndpoint"),
        config.get("spEntityId"),
    )


def _normalize_configuration_id(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value.get("id")
    if isinstance(value, list) and value:
        return _normalize_configuration_id(value[0])
    return None


def get_sso_configurations(client, tenant_id):
    url = f"{client.base_url}{SSO_ENDPOINT}"
    response = client.session.get(url, headers=_headers(client, tenant_id))
    response.raise_for_status()
    return _extract_items(response.json())


def get_sso_default_roles(client, tenant_id, configuration_id):
    url = f"{client.base_url}{SSO_ROLES_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.get(url, headers=_headers(client, tenant_id))
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        if isinstance(data.get("roleIds"), list):
            return data.get("roleIds")
        if isinstance(data.get("items"), list):
            return data.get("items")
    if isinstance(data, list):
        return data
    return []


def set_sso_default_roles(client, tenant_id, configuration_id, role_ids):
    url = f"{client.base_url}{SSO_ROLES_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.put(
        url,
        headers=_headers(client, tenant_id),
        json={"roleIds": role_ids},
    )
    response.raise_for_status()


def get_sso_groups(client, tenant_id, configuration_id):
    url = f"{client.base_url}{SSO_GROUPS_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.get(url, headers=_headers(client, tenant_id))
    response.raise_for_status()
    return _extract_items(response.json())


def create_sso_group(client, tenant_id, configuration_id, payload):
    url = f"{client.base_url}{SSO_GROUPS_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.post(url, headers=_headers(client, tenant_id), json=payload)
    response.raise_for_status()


def get_sso_domains(client, tenant_id, configuration_id):
    url = f"{client.base_url}{SSO_DOMAINS_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.get(url, headers=_headers(client, tenant_id))
    response.raise_for_status()
    return _extract_items(response.json())


def create_sso_domain(client, tenant_id, configuration_id, payload):
    url = f"{client.base_url}{SSO_DOMAINS_ENDPOINT.format(configuration_id=configuration_id)}"
    response = client.session.post(url, headers=_headers(client, tenant_id), json=payload)
    response.raise_for_status()


def get_sso_domains_configuration(client, tenant_id):
    url = f"{client.base_url}{SSO_DOMAINS_CONFIG_ENDPOINT}"
    response = client.session.get(url, headers=_headers(client, tenant_id))
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def set_sso_domains_configuration(client, tenant_id, domains_configuration):
    payload = {
        "allowVerifiedUsersToAddDomains": domains_configuration.get("allowVerifiedUsersToAddDomains", False),
        "skipDomainVerification": domains_configuration.get("skipDomainVerification", False),
        "bypassDomainCrossValidation": domains_configuration.get("bypassDomainCrossValidation", False),
    }
    url = f"{client.base_url}{SSO_DOMAINS_CONFIG_ENDPOINT}"
    response = client.session.put(url, headers=_headers(client, tenant_id), json=payload)
    response.raise_for_status()

def get_multiple_sso_per_domain_configuration(client):
    url = f"{client.base_url}{SSO_MULTIPLE_PER_DOMAIN_ENDPOINT}"
    response = client.session.get(url, headers=_headers(client))
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def set_multiple_sso_per_domain_configuration(client, configuration):
    payload = {
        "active": bool(configuration.get("active", False)),
        "unspecifiedTenantStrategy": configuration.get("unspecifiedTenantStrategy", "FIRST_CREATED"),
        "useActiveTenant": bool(configuration.get("useActiveTenant", False)),
    }
    url = f"{client.base_url}{SSO_MULTIPLE_PER_DOMAIN_ENDPOINT}"
    response = client.session.put(url, headers=_headers(client), json=payload)
    response.raise_for_status()


def _sync_multiple_sso_per_domain_configuration(source_client, destination_client):
    source_configuration = get_multiple_sso_per_domain_configuration(source_client)
    if not source_configuration:
        return
    set_multiple_sso_per_domain_configuration(destination_client, source_configuration)


def _build_create_payload(source_config):
    payload = {}
    for key in CREATE_SSO_FIELDS:
        if key in source_config:
            payload[key] = source_config[key]
    return payload


def _create_sso_from_metadata(client, tenant_id, source_config):
    metadata = source_config.get("metadata")
    if not isinstance(metadata, str) or not metadata.strip():
        return None

    url = f"{client.base_url}{SSO_METADATA_ENDPOINT}"
    response = client.session.post(
        url,
        headers=_headers(client, tenant_id),
        json={"metadata": metadata},
    )
    response.raise_for_status()
    return _normalize_configuration_id(response.json())


def _create_sso_configuration(client, tenant_id, source_config):
    url = f"{client.base_url}{SSO_ENDPOINT}"
    payload = _build_create_payload(source_config)
    if not payload:
        return False, None, "empty-payload"

    try:
        response = client.session.post(
            url,
            headers=_headers(client, tenant_id),
            json=payload,
        )
        response.raise_for_status()
        config_id = _normalize_configuration_id(response.json())
        return True, config_id, "created"
    except requests.exceptions.HTTPError as create_error:
        # Fallback: metadata-based creation if available.
        try:
            config_id = _create_sso_from_metadata(client, tenant_id, source_config)
            if config_id:
                return True, config_id, "created-via-metadata"
        except Exception:
            pass

        status = create_error.response.status_code if create_error.response is not None else "unknown"
        return False, None, f"http-{status}"


def _build_destination_role_lookups(destination_client):
    destination_roles = get_roles(destination_client, split=False) or []
    by_tenant_key = {}
    by_tenant_name = {}
    for role in destination_roles:
        tenant_id = role.get("tenantId")
        role_key = role.get("key")
        role_name = role.get("name")
        role_id = role.get("id")
        if role_id and role_key is not None:
            by_tenant_key[(tenant_id, role_key)] = role_id
        if role_id and role_name is not None:
            by_tenant_name[(tenant_id, role_name)] = role_id
    return by_tenant_key, by_tenant_name


def _map_role_ids(source_role_ids, source_roles_by_id, destination_by_tenant_key, destination_by_tenant_name):
    mapped = []
    for source_role_id in source_role_ids:
        source_role = source_roles_by_id.get(source_role_id)
        if not source_role:
            continue

        source_tenant = source_role.get("tenantId")
        source_key = source_role.get("key")
        source_name = source_role.get("name")

        destination_role_id = None
        lookup_tenants = [source_tenant, None] if source_tenant is not None else [None]

        for tenant_key in lookup_tenants:
            if source_key is not None:
                destination_role_id = destination_by_tenant_key.get((tenant_key, source_key))
            if destination_role_id:
                break
            if source_name is not None:
                destination_role_id = destination_by_tenant_name.get((tenant_key, source_name))
            if destination_role_id:
                break

        if destination_role_id:
            mapped.append(destination_role_id)
    return sorted(set(mapped))


def _sync_sso_domains_configuration(source_client, destination_client, tenant_id):
    try:
        source_domains_configuration = get_sso_domains_configuration(source_client, tenant_id)
        if not source_domains_configuration:
            return
        set_sso_domains_configuration(destination_client, tenant_id, source_domains_configuration)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status in (403, 404):
            return
        raise


def _normalize_domain_payload(source_domain):
    payload = {}
    ignored_keys = {
        "id",
        "configurationId",
        "tenantId",
        "vendorId",
        "createdAt",
        "updatedAt",
        "validated",
        "validatedAt",
    }
    for key, value in source_domain.items():
        if key in ignored_keys:
            continue
        payload[key] = value
    return payload


def _sync_sso_roles(source_client, destination_client, tenant_id, source_config_id, destination_config_id,
                    source_roles_by_id, destination_by_tenant_key, destination_by_tenant_name):
    source_default_role_ids = get_sso_default_roles(source_client, tenant_id, source_config_id)
    if not source_default_role_ids:
        return 0

    mapped_role_ids = _map_role_ids(
        source_default_role_ids,
        source_roles_by_id,
        destination_by_tenant_key,
        destination_by_tenant_name,
    )
    if not mapped_role_ids:
        return 0

    set_sso_default_roles(destination_client, tenant_id, destination_config_id, mapped_role_ids)
    return len(mapped_role_ids)


def _sync_sso_groups(source_client, destination_client, tenant_id, source_config_id, destination_config_id,
                     source_roles_by_id, destination_by_tenant_key, destination_by_tenant_name):
    source_groups = get_sso_groups(source_client, tenant_id, source_config_id)
    if not source_groups:
        return 0, 0

    destination_groups = get_sso_groups(destination_client, tenant_id, destination_config_id)
    destination_group_names = {
        group.get("group")
        for group in destination_groups
        if isinstance(group, dict) and group.get("group")
    }

    created = 0
    skipped = 0
    for source_group in source_groups:
        group_name = source_group.get("group")
        if not group_name:
            skipped += 1
            continue
        if group_name in destination_group_names:
            skipped += 1
            continue

        source_role_ids = source_group.get("roleIds", [])
        mapped_role_ids = _map_role_ids(
            source_role_ids,
            source_roles_by_id,
            destination_by_tenant_key,
            destination_by_tenant_name,
        )
        payload = {"group": group_name, "roleIds": mapped_role_ids}
        create_sso_group(destination_client, tenant_id, destination_config_id, payload)
        created += 1

    return created, skipped


def _extract_source_domains_from_configuration(source_config):
    """
    Domains should be read from the SSO configuration payload returned by:
    GET /team/resources/sso/v1/configurations
    """
    for key in ("domains", "ssoDomains", "configurationDomains"):
        value = source_config.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return [item for item in value["items"] if isinstance(item, dict)]
    return []


def _sync_sso_domains(destination_client, tenant_id, source_config, destination_config_id):
    source_domains = _extract_source_domains_from_configuration(source_config)
    if not source_domains:
        return 0, 0

    created = 0
    skipped = 0
    for source_domain in source_domains:
        payload = _normalize_domain_payload(source_domain)
        if not payload:
            skipped += 1
            continue
        try:
            create_sso_domain(destination_client, tenant_id, destination_config_id, payload)
            created += 1
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            response_text = (e.response.text or "").lower() if e.response is not None else ""
            # Domain already exists / duplicate domain should not fail the migration.
            if status in (400, 409) and (
                "already" in response_text or "exists" in response_text or "duplicate" in response_text
            ):
                skipped += 1
                continue
            raise

    return created, skipped


def migrate_sso_configurations(source_client, destination_client):
    logger = get_logger()
    logger.info("Starting SSO configurations migration")
    log_subsection("Migrating SSO Configurations")

    try:
        _sync_multiple_sso_per_domain_configuration(source_client, destination_client)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status in (403, 404):
            log_warning(f"Skipping multiple-sso-per-domain sync (status={status}).")
        else:
            log_warning(f"Failed syncing multiple-sso-per-domain configuration: {e}")
    except Exception as e:
        log_warning(f"Failed syncing multiple-sso-per-domain configuration: {e}")

    source_tenants = get_tenants(source_client)
    if not source_tenants:
        log_warning("No source tenants found; skipping SSO configuration migration.")
        return

    source_roles = get_roles(source_client, split=False) or []
    source_roles_by_id = {role.get("id"): role for role in source_roles if role.get("id")}
    destination_by_tenant_key, destination_by_tenant_name = _build_destination_role_lookups(destination_client)

    configurations_migrated = 0
    configurations_skipped = 0
    configurations_failed = 0
    groups_created = 0
    groups_skipped = 0
    domains_created = 0
    domains_skipped = 0
    role_assignments_applied = 0

    for tenant in source_tenants:
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            configurations_skipped += 1
            continue

        try:
            _sync_sso_domains_configuration(source_client, destination_client, tenant_id)
        except Exception as e:
            log_warning(f"Skipping domains configuration sync for tenant {tenant_id}: {e}")

        try:
            source_configs = get_sso_configurations(source_client, tenant_id)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            # 404/403 often means SSO is not enabled for the tenant/account.
            if status in (403, 404):
                configurations_skipped += 1
                continue
            configurations_failed += 1
            log_error(f"Failed fetching source SSO configs for tenant {tenant_id}: {e}")
            continue
        except Exception as e:
            configurations_failed += 1
            log_error(f"Failed fetching source SSO configs for tenant {tenant_id}: {e}")
            continue

        if not source_configs:
            configurations_skipped += 1
            continue

        try:
            destination_configs = get_sso_configurations(destination_client, tenant_id)
        except Exception:
            destination_configs = []
        destination_by_signature = {
            _sso_signature(cfg): cfg
            for cfg in destination_configs
            if isinstance(cfg, dict)
        }

        for source_config in source_configs:
            source_config_id = source_config.get("id")
            signature = _sso_signature(source_config)
            destination_config = destination_by_signature.get(signature)
            destination_config_id = destination_config.get("id") if destination_config else None

            if not destination_config_id:
                success, destination_config_id, mode = _create_sso_configuration(
                    destination_client, tenant_id, source_config
                )
                if not success:
                    configurations_failed += 1
                    log_error(
                        f"Failed creating SSO config for tenant {tenant_id} "
                        f"(type={source_config.get('type')}, mode={mode})."
                    )
                    continue

                # If API response did not return config id, refetch and resolve by signature.
                if not destination_config_id:
                    try:
                        updated_destination_configs = get_sso_configurations(destination_client, tenant_id)
                        for cfg in updated_destination_configs:
                            if _sso_signature(cfg) == signature:
                                destination_config_id = cfg.get("id")
                                break
                    except Exception:
                        pass

                if not destination_config_id:
                    configurations_failed += 1
                    log_error(
                        f"Created SSO config but couldn't resolve destination ID for tenant {tenant_id} "
                        f"(type={source_config.get('type')})."
                    )
                    continue

                configurations_migrated += 1
            else:
                configurations_skipped += 1

            if not source_config_id:
                log_warning(
                    f"Source SSO configuration has no id for tenant {tenant_id}; "
                    f"skipping roles/groups/domains sync for destination config {destination_config_id}."
                )
                continue

            # Sync roles/groups/domains for each source configuration.
            try:
                role_assignments_applied += _sync_sso_roles(
                    source_client,
                    destination_client,
                    tenant_id,
                    source_config_id,
                    destination_config_id,
                    source_roles_by_id,
                    destination_by_tenant_key,
                    destination_by_tenant_name,
                )
            except Exception as e:
                log_warning(
                    f"Failed syncing SSO default roles for tenant {tenant_id}, "
                    f"config {destination_config_id}: {e}"
                )

            try:
                created, skipped = _sync_sso_groups(
                    source_client,
                    destination_client,
                    tenant_id,
                    source_config_id,
                    destination_config_id,
                    source_roles_by_id,
                    destination_by_tenant_key,
                    destination_by_tenant_name,
                )
                groups_created += created
                groups_skipped += skipped
            except Exception as e:
                log_warning(
                    f"Failed syncing SSO groups for tenant {tenant_id}, "
                    f"config {destination_config_id}: {e}"
                )

            try:
                created, skipped = _sync_sso_domains(
                    destination_client,
                    tenant_id,
                    source_config,
                    destination_config_id,
                )
                domains_created += created
                domains_skipped += skipped
            except Exception as e:
                log_warning(
                    f"Failed syncing SSO domains for tenant {tenant_id}, "
                    f"config {destination_config_id}: {e}"
                )

    logger.print_stats("SSO Configurations Migration Summary", {
        "Tenants Checked": len(source_tenants),
        "Configurations Migrated": configurations_migrated,
        "Configurations Skipped": configurations_skipped,
        "Configurations Failed": configurations_failed,
        "Default Roles Applied": role_assignments_applied,
        "Groups Created": groups_created,
        "Groups Skipped": groups_skipped,
        "Domains Created": domains_created,
        "Domains Skipped": domains_skipped,
    })
    log_success("SSO configurations migration completed")
