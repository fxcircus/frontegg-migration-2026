# Frontegg Account Migration Tool

Migrates configuration from one Frontegg account (source) to another
(destination) using the Frontegg API: tenants, tenant hierarchy, roles &
permissions, applications, security rules, email configuration, prehooks,
allowed origins, redirect URIs, JWT settings, SSO configurations, entitlements,
API access control, user tenant memberships & roles, and API tokens.

> **Migrating users?** User records themselves — including password hashes and
> MFA secrets — are migrated by the Frontegg team, because those secrets cannot
> be read from the source account through the public API. Contact Frontegg
> Support and we will migrate your users for you.
>
> **Everything around** the users you can do yourself with this tool. Once the
> users exist in the destination, the `post-user-migration` profile adds each
> user's remaining tenant memberships with the roles they held on each tenant,
> and recreates their API tokens. See
> [User tenant memberships & roles](#8-user-tenant-memberships--roles) and
> [API tokens](#9-api-tokens).

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
| `full-migration` | Migrate all supported configuration, including tenant hierarchy (no deletions) |
| `post-user-migration` | Run **after** Support migrates your users: tenant memberships, per-tenant roles, and API tokens |
| `user-tenant-roles-only` | Only the missing user tenant memberships and their roles |
| `api-tokens-only` | Only tenant and user API tokens |
| `roles-only` | Categories, permissions, and roles only |
| `delete-cleanup` | **DESTRUCTIVE** — delete data from the destination account |

The migration splits into two phases because of the user dependency:

```
  1. python3 orchestrator/run.py --profile full-migration
  2. Frontegg Support migrates your users
  3. python3 orchestrator/run.py --profile post-user-migration
```

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
MIGRATE_TENANT_HIERARCHY=true

# Require the users to already exist in the destination account:
MIGRATE_USER_TENANT_ROLES=true
MIGRATE_TENANT_API_TOKENS=true
MIGRATE_USER_API_TOKENS=true
```

Optional tuning:

```
# Bulk invite is limited to 5/min per environment on Launch, 10/min on
# Scale/Enterprise. Default pace is one request per 12s; set 6 on Scale+.
BULK_INVITE_MIN_INTERVAL_SECONDS=12

# Recreate tokens whose expiry has already passed (default false).
MIGRATE_EXPIRED_API_TOKENS=false

# Where new API token credentials are written (created with mode 0600).
API_TOKENS_OUTPUT_CSV=migrated_tenant_api_tokens.csv
USER_API_TOKENS_OUTPUT_CSV=migrated_user_api_tokens.csv
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
- Tenant hierarchy (parent/child account relationships)
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
- **User tenant memberships and their per-tenant roles** (requires the users to
  exist in the destination first — see §8)
- **Tenant API tokens** and **user API tokens** — recreated with **new**
  clientId/secret (see §9)

**Migrated by Frontegg Support (contact us):**
- Users (including password hashes and MFA phone numbers)
- Groups

**Not migrated:**
- Builder settings
- Custom login, user pools
- Webhooks — they cannot be created through the API
- Flows
- Integrations (except email provider)
- Audit trail
- Other environment settings

---

## 8. User tenant memberships & roles

Support migrates the user records. What that leaves behind is users who belong
to several accounts (tenants) in the source but only one in the destination, and
the roles they held on each of those tenants. `MIGRATE_USER_TENANT_ROLES` closes
that gap.

It does **not** create users. Any source user missing from the destination is
reported and skipped — migrate the users first.

How it works:

1. One sweep of `/identity/resources/users/v3` per account returns every user
   together with a `tenants` array of `{tenantId, roles[]}`, i.e. the entire
   membership graph.
2. Source role IDs are translated to destination role IDs by matching on
   **(tenant scope, role key)**. Role IDs are generated per environment and are
   never portable; role *names* carry no uniqueness guarantee. Tenants keep their
   original IDs, so a tenant-scoped role maps to the same tenant.
3. Missing memberships are grouped per tenant and sent as one bulk invite each,
   with `skipInviteEmail` so nobody receives an email.

**Re-runnable.** It compares live state on both sides every run, so memberships
that already exist are skipped. The invite endpoint is asynchronous (`202` plus a
job id), so run it a second time to confirm it reports nothing left to do.

**Pacing.** Bulk invite is limited to **5/min per environment** on Launch and
10/min on Scale/Enterprise. That limit is per environment, not per IP, so running
several workers cannot make it faster. The step paces itself to one request per
12 seconds by default and backs off on `429`; lower
`BULK_INVITE_MIN_INTERVAL_SECONDS` to `6` on Scale/Enterprise.

Reported but not changed:
- Memberships that exist in the destination but whose roles differ from the
  source — logged as drift, since re-inviting would not remove or replace roles.
- Memberships that carry **no** roles in the source. The API requires `roleIds`
  or `permissionIds`, so these are listed for manual handling.

---

## 9. API tokens

> **New credentials are unavoidable.** Frontegg generates the `clientId` and
> `secret` server-side and returns the secret exactly once, at creation. No
> endpoint accepts an existing pair and there is no import route, so every
> migrated token is new and **every consumer must be re-pointed**.

Because the secret cannot be retrieved later, each new token is written to a CSV
the moment it is created, alongside the source `clientId` it replaces:

| Flag | Output CSV |
|------|-----------|
| `MIGRATE_TENANT_API_TOKENS` | `migrated_tenant_api_tokens.csv` |
| `MIGRATE_USER_API_TOKENS` | `migrated_user_api_tokens.csv` |

Both files are created with mode `0600` and are git-ignored. Treat them as
secrets: hand them over securely, then delete your local copy. If a file is lost,
the only recovery is deleting the tokens and creating them again.

**Tenant API tokens** keep their description, metadata, expiry and roles, with
role IDs remapped to the destination. Tokens whose expiry has already passed are
skipped unless `MIGRATE_EXPIRED_API_TOKENS=true`.

**User API tokens** carry no roles of their own — a user token inherits whatever
roles the user holds on that tenant — so run `MIGRATE_USER_TENANT_ROLES` first or
the new token will resolve to the wrong permissions.

**Cost.** Neither token type has a cross-tenant listing endpoint. Tenant tokens
cost one call per tenant. User tokens are worse: the list endpoint requires both
`frontegg-tenant-id` and `frontegg-user-id`, so discovery costs one call per
(user, tenant) membership. On a large account that is thousands of calls, which
is why `MIGRATE_USER_API_TOKENS` is off by default.

---

## 10. Troubleshooting

- **Authentication failed** — verify `CLIENT_ID_*` / `API_KEY_*` and that the
  `BASE_URL_*` region is correct and has no trailing slash.
- **Tenant-scoped role not created** — ensure the tenant exists in the
  destination (run with `MIGRATE_TENANTS=true` in the same run).
- **Many "permission already removed (cascade/system)" lines during deletion**
  — expected; deleting some permissions cascades to related ones, and system
  permissions are protected.
- **Rate limit (429) warnings** — the tool throttles and retries
  automatically; long runs on large accounts are normal.
- **"users do not exist in the destination"** during
  `MIGRATE_USER_TENANT_ROLES` — this step never creates users. Have Support
  migrate the users first, then re-run.
- **"tenants referenced by source memberships do not exist"** — run with
  `MIGRATE_TENANTS=true` first; memberships cannot be created for a missing
  tenant.
- **"roles could not be mapped"** — the source role has no destination
  equivalent with the same key in the same scope. Run `MIGRATE_ROLES` first.
- **Membership migration reports nothing changed right after a run** — bulk
  invite is asynchronous, so the memberships may still be landing. Re-run after a
  minute; the step is idempotent.
- **Hierarchy edge fails with 400** — the child already belongs to another
  parent, or parent and child are the same tenant. Frontegg allows a tenant only
  one parent.
- **Lost an API token CSV** — the secrets are unrecoverable. Delete the affected
  tokens in the destination and re-run the step to issue new ones.

Questions or issues? Contact Frontegg Support.
