"""Configuration resolver for the migration orchestrator.

Responsibilities:
- Hold the canonical list of migration / deletion flags.
- Load named profiles from profiles.json.
- Resolve effective config with precedence: defaults < profile < CLI overrides.
- Apply resolved migration/deletion flags into the process environment so the
  existing scripts (which read os.getenv at import time) pick them up.

Secrets/credentials are intentionally NOT handled here; they stay in .env.
"""

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

# Canonical migration flags, in the order the migration runs them.
MIGRATION_FLAGS = [
    "MIGRATE_TENANTS",
    "MIGRATE_CATEGORIES",
    "MIGRATE_PERMISSIONS",
    "MIGRATE_ROLES",
    "MIGRATE_APPLICATIONS",
    "MIGRATE_SECURITY_RULES",
    "MIGRATE_EMAIL_TEMPLATES",
    "MIGRATE_EMAIL_SENDER",
    "MIGRATE_PREHOOKS",
    "MIGRATE_ALLOWED_ORIGINS",
    "MIGRATE_REDIRECT_URIS",
    "MIGRATE_JWT_SETTINGS",
    "MIGRATE_SSO_CONFIGURATIONS",
    "MIGRATE_ENTITLEMENTS",
    "MIGRATE_API_ACCESS_CONTROL",
]

DELETION_FLAGS = [
    "DELETE_TENANTS",
    "DELETE_USERS",
    "DELETE_PERMISSIONS",
    "DELETE_ROLES",
    "DELETE_APPLICATIONS",
    "DELETE_PREHOOKS",
]

_TRUE_VALUES = {"true", "1", "yes", "y", "on"}
_FALSE_VALUES = {"false", "0", "no", "n", "off"}


class ConfigError(Exception):
    """Raised for invalid profiles, flags, or override values."""


def _canonical_flag(name):
    """Normalize a flag name (trim + uppercase)."""
    return name.strip().upper()


def parse_bool(value):
    """Parse a human-friendly boolean string/value."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"Invalid boolean value '{value}'. Use one of: true/false, yes/no, 1/0, on/off."
    )


def load_profiles(path=PROFILES_PATH):
    """Load the profiles JSON file and return the profiles mapping."""
    if not os.path.exists(path):
        raise ConfigError(f"Profiles file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Could not parse profiles file {path}: {e}") from e

    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError(f"No 'profiles' object found in {path}")
    return profiles


def _default_config():
    return {
        "profile": None,
        "description": "",
        "migrate": {flag: False for flag in MIGRATION_FLAGS},
        "delete": {flag: False for flag in DELETION_FLAGS},
    }


def _apply_profile(config, profile_name, profiles):
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        raise ConfigError(
            f"Unknown profile '{profile_name}'. Available profiles: {available}"
        )

    profile = profiles[profile_name]
    config["profile"] = profile_name
    config["description"] = profile.get("description", "")

    for raw_flag, value in profile.get("migrate", {}).items():
        flag = _canonical_flag(raw_flag)
        if flag not in config["migrate"]:
            raise ConfigError(
                f"Profile '{profile_name}' references unknown migration flag '{raw_flag}'."
            )
        config["migrate"][flag] = parse_bool(value)

    for raw_flag, value in profile.get("delete", {}).items():
        flag = _canonical_flag(raw_flag)
        if flag not in config["delete"]:
            raise ConfigError(
                f"Profile '{profile_name}' references unknown deletion flag '{raw_flag}'."
            )
        config["delete"][flag] = parse_bool(value)

    return config


def _apply_overrides(config, overrides):
    """Apply --set KEY=VALUE overrides onto the resolved config."""
    for raw in overrides or []:
        if "=" not in raw:
            raise ConfigError(f"Invalid override '{raw}'. Expected format KEY=VALUE.")
        raw_key, raw_value = raw.split("=", 1)
        flag = _canonical_flag(raw_key)
        value = parse_bool(raw_value)
        if flag in config["migrate"]:
            config["migrate"][flag] = value
        elif flag in config["delete"]:
            config["delete"][flag] = value
        else:
            known = ", ".join(MIGRATION_FLAGS + DELETION_FLAGS)
            raise ConfigError(
                f"Unknown flag '{raw_key}' in override. Known flags: {known}"
            )
    return config


def resolve_config(profile_name=None, overrides=None, profiles=None):
    """Build the effective run configuration.

    Precedence: defaults (all off) < profile < CLI overrides.
    """
    if profiles is None:
        profiles = load_profiles()

    config = _default_config()
    if profile_name:
        config = _apply_profile(config, profile_name, profiles)
    config = _apply_overrides(config, overrides)

    return config


def apply_to_env(config):
    """Write resolved migration/deletion flags into os.environ.

    This is what makes the existing scripts honor the resolved profile, since
    they read os.getenv at import time. Credentials in .env are untouched.
    """
    for flag, value in config["migrate"].items():
        os.environ[flag] = "true" if value else "false"
    for flag, value in config["delete"].items():
        os.environ[flag] = "true" if value else "false"


def enabled_migration_steps(config):
    return [flag for flag in MIGRATION_FLAGS if config["migrate"].get(flag)]


def enabled_deletion_steps(config):
    return [flag for flag in DELETION_FLAGS if config["delete"].get(flag)]


def has_deletions(config):
    return any(config["delete"].values())
