# Frontegg Account Migration Tool

Migrates configuration from one Frontegg account (source) to another
(destination) using the Frontegg API: tenants, roles & permissions,
applications, security rules, email configuration, prehooks, allowed origins,
redirect URIs, JWT settings, SSO configurations, entitlements, and API access
control.

> **Migrating users?** User, group, and role-assignment data is migrated
> separately by the Frontegg team. Contact Frontegg Support and we will
> migrate your users for you. Everything else can be migrated with this tool,
> at any time, on your own.

---

## 1. Prerequisites

- Python 3.9+ and `pip`.
- Vendor API credentials (**Client ID** + **API key**) for **both** the source
  and destination Frontegg accounts. You can find these in the Frontegg Portal
  under **[Environment] → Env settings**.

---

## 2. Install

Clone the repository and install dependencies in a virtual environment:

```bash
git clone https://github.com/fxcircus/frontegg-migration-2026.git
cd frontegg-migration-2026

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Configure credentials (`.env`)

Copy the example environment file and add your credentials:

```bash
cp .env.example .env   # then edit .env
```

Required values:

```
# Source account (FROM)
BASE_URL_1=https://api.frontegg.com
CLIENT_ID_1=<source-client-id>
API_KEY_1=<source-api-key>

# Destination account (TO)
BASE_URL_2=https://api.us.frontegg.com
CLIENT_ID_2=<destination-client-id>
API_KEY_2=<destination-api-key>
```

**Available regions:**

| Region | Base URL |
|--------|----------|
| EU | `https://api.frontegg.com` |
| US | `https://api.us.frontegg.com` |
| CA | `https://api.ca.frontegg.com` |
| AU | `https://api.au.frontegg.com` |

> `BASE_URL` values must **not** end with a trailing slash.
> Your `.env` file is git-ignored — never commit real credentials.

---

## 4. Run the migration

### Option A — Orchestrator (recommended)

One command, driven by a named profile, with a preflight summary and a
confirmation prompt before anything is changed.

List the available profiles:

```bash
python3 orchestrator/run.py --list-profiles
```

Preview a run first (no changes are made):

```bash
python3 orchestrator/run.py --profile full-migration --dry-run
```

Then run it:

```bash
python3 orchestrator/run.py --profile full-migration
```

Built-in profiles:

| Profile | Purpose |
|---------|---------|
| `full-migration` | Migrate all supported configuration (no deletions) |
| `roles-only` | Categories, permissions, and roles only |
| `delete-cleanup` | **DESTRUCTIVE** — delete data from the destination account |

Override individual flags on top of a profile (repeatable):

```bash
python3 orchestrator/run.py --profile roles-only --set MIGRATE_TENANTS=true
```

### Option B — Manual

Set the boolean flags in `.env`, then run:

```bash
python3 main.py
```

Migration flags (`.env`):

```
MIGRATE_TENANTS=true
MIGRATE_CATEGORIES=true
MIGRATE_PERMISSIONS=true
MIGRATE_ROLES=true
MIGRATE_APPLICATIONS=true
MIGRATE_SECURITY_RULES=true
MIGRATE_EMAIL_TEMPLATES=true
MIGRATE_EMAIL_SENDER=true
MIGRATE_PREHOOKS=true
MIGRATE_ALLOWED_ORIGINS=true
MIGRATE_REDIRECT_URIS=true
MIGRATE_JWT_SETTINGS=true
MIGRATE_SSO_CONFIGURATIONS=true
MIGRATE_ENTITLEMENTS=true
MIGRATE_API_ACCESS_CONTROL=true
```

> **Order matters:** tenants are migrated before roles so that tenant-scoped
> roles attach to the correct (same-ID) tenant. When migrating tenant-scoped
> roles, keep `MIGRATE_TENANTS=true` in the same run. Tenants are always
> created with their original IDs, which is what keeps everything wired up
> correctly across accounts.

Re-runs are safe: resources that already exist in the destination are skipped.

---

## 5. Reverting / deleting destination data

**Destructive.** Always deletes from the **destination** account
(`BASE_URL_2` / `CLIENT_ID_2`). Use this to reset the destination if you want
to start over.

Via the orchestrator (recommended — has guards):

```bash
python3 orchestrator/run.py --profile delete-cleanup --dry-run   # preview
python3 orchestrator/run.py --profile delete-cleanup             # type DELETE to confirm
```

Delete only specific resource types:

```bash
python3 orchestrator/run.py --profile delete-cleanup \
  --set DELETE_TENANTS=false --set DELETE_APPLICATIONS=false
```

Or manually, with `DELETE_*` flags in `.env`:

```bash
python3 delete_account_data.py
```

Deletion flags: `DELETE_TENANTS`, `DELETE_USERS`, `DELETE_PERMISSIONS`,
`DELETE_ROLES`, `DELETE_APPLICATIONS`, `DELETE_PREHOOKS`.

---

## 6. Logs

- Migration runs write to `migration.log` in the project root (overwritten
  each run).
- Deletions write to a separate `delete.log` (appended each run).
- Console output is colorized.

---

## 7. What gets migrated

**Migrated by this tool:**
- Tenants (Accounts) — preserving original tenant IDs
- Categories and Permissions
- Roles (account-level and tenant-scoped, including same key across tenants)
- Applications (app creation, default app handling, tenant/user assignments)
- Security Rules
- Email provider/sender configuration and email templates
- Prehooks
- Allowed Origins
- Redirect URIs
- JWT settings (token/refresh expiration, cookie settings)
- SSO configurations (roles/groups/domains/domain config)
- Entitlements: features, plans, plan-feature links, assignments, feature flags
- API access control routes

**Migrated by Frontegg Support (contact us):**
- Users (including password hashes and MFA phone numbers)
- User role assignments
- Groups

**Not migrated:**
- Builder settings
- Tenant settings (hierarchies, API tokens, custom login, …)
- User pools
- Webhooks
- Flows
- Integrations (except email provider)
- Audit trail
- Other environment settings

---

## 8. Troubleshooting

- **Authentication failed** — verify `CLIENT_ID_*` / `API_KEY_*` and that the
  `BASE_URL_*` region is correct and has no trailing slash.
- **Tenant-scoped role not created** — ensure the tenant exists in the
  destination (run with `MIGRATE_TENANTS=true` in the same run).
- **Many "permission already removed (cascade/system)" lines during deletion**
  — expected; deleting some permissions cascades to related ones, and system
  permissions are protected.
- **Rate limit (429) warnings** — the tool throttles and retries
  automatically; long runs on large accounts are normal.

Questions or issues? Contact Frontegg Support.
