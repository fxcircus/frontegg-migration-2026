# Frontegg Account Migration Tool

Migrates configuration from one Frontegg account (source) to another
(destination) using the Frontegg API: tenants, tenant hierarchy, roles &
permissions, applications, security rules, email configuration, prehooks,
allowed origins, redirect URIs, JWT settings, auth/security policies (MFA
enforcement incl. per-tenant forced MFA, MFA methods, lockout, password
complexity/history, captcha, session management), SSO configurations,
entitlements, API access control, user groups, user tenant memberships &
roles, user attributes (lock state, phone, metadata), cutover-delta users, and
API tokens. A read-only `verify_migration.py` reconciles the two accounts and
reports every difference.

**The source account is never written to.** The source client is opened
read-only and its HTTP session refuses every mutating request. The one
exception is opt-in: moving tenant API tokens between two environments in the
same region with their original credentials, which deletes each moved token
from the source (`TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true`, see
[API tokens](#9-api-tokens)).

> **Migrating users?** User records themselves — including password hashes and
> MFA secrets — are migrated by the Frontegg team, because those secrets cannot
> be read from the source account through the public API. Contact Frontegg
> Support and we will migrate your users for you.
>
> **Everything around** the users you can do yourself with this tool. Once the
> users exist in the destination, the `post-user-migration` profile adds each
> user's remaining tenant memberships with the roles they held on each tenant,
> and migrates their API tokens. See
> [User tenant memberships & roles](#8-user-tenant-memberships--roles) and
> [API tokens](#9-api-tokens).

> **Keeping tenant API token credentials?** By default every migrated tenant
> API token gets a new clientId and secret. To keep the original ones
> (`TENANT_API_TOKENS_MODE=preserve`), Frontegg must first enable the feature
> flag `tenant-api-token-export-import-enabled` on **both** the source and the
> destination environment. Contact Frontegg Support and we will enable it for
> you. See [Preserve mode](#preserve-mode-original-credentials).

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
| `full-migration` | Migrate all supported configuration, including tenant hierarchy and auth policies (no deletions) |
| `post-user-migration` | Run **after** Support migrates your users: tenant memberships, per-tenant roles, groups, user attributes, and API tokens |
| `cutover-delta` | At cutover: create users added since the export (no passwords — see §11), then their memberships, groups, and attributes |
| `user-tenant-roles-only` | Only the missing user tenant memberships and their roles |
| `groups-only` | Only user groups (definitions, group roles, members) |
| `user-attributes-only` | Only user attributes (lock state, phone, metadata) |
| `auth-policies-only` | Only auth/security policies (MFA, lockout, password, captcha, sessions) |
| `api-tokens-only` | Only tenant and user API tokens (tenant tokens follow `TENANT_API_TOKENS_MODE`, see §9) |
| `roles-only` | Categories, permissions, and roles only |
| `delete-cleanup` | **DESTRUCTIVE** — delete data from the destination account |

The migration splits into phases because of the user dependency:

```
  1. python3 orchestrator/run.py --profile full-migration
  2. Frontegg Support migrates your users
  3. python3 orchestrator/run.py --profile post-user-migration
  4. python3 verify_migration.py            # read-only reconciliation report
  5. (at cutover) python3 orchestrator/run.py --profile cutover-delta
     ...or ask Support for a delta user migration, then re-run step 3
  6. python3 verify_migration.py            # confirm nothing is left behind
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
MIGRATE_AUTH_POLICIES=true

# OPT-IN cutover delta: creates source-only users WITHOUT passwords (see §11):
MIGRATE_MISSING_USERS=false

# Require the users to already exist in the destination account:
MIGRATE_USER_TENANT_ROLES=true
MIGRATE_GROUPS=true
MIGRATE_USER_ATTRIBUTES=true
MIGRATE_TENANT_API_TOKENS=true
MIGRATE_USER_API_TOKENS=true
```

Optional tuning:

```
# Bulk invite is limited to 5/min per environment on Launch, 10/min on
# Scale/Enterprise. Default pace is one request per 12s; set 6 on Scale+.
BULK_INVITE_MIN_INTERVAL_SECONDS=12

# Migrate tokens whose expiry has already passed (default false).
MIGRATE_EXPIRED_API_TOKENS=false

# Where new API token credentials are written (created with mode 0600).
API_TOKENS_OUTPUT_CSV=migrated_tenant_api_tokens.csv
USER_API_TOKENS_OUTPUT_CSV=migrated_user_api_tokens.csv

# Tenant API tokens: recreate (default, NEW credentials) or preserve (ORIGINAL
# credentials, needs a Frontegg feature flag). See §9 before changing these.
TENANT_API_TOKENS_MODE=recreate
TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=false
TENANT_API_TOKENS_BACKUP_DIR=tenant_api_token_backups
```

These settings live only in `.env`; profiles and `--set` control the
`MIGRATE_*` / `DELETE_*` flags. The orchestrator's preflight shows which tenant
API token mode will run.

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
- **Auth/security policies** — MFA enforcement (incl. **per-tenant forced
  MFA**), MFA method config, lockout, password complexity & history, captcha,
  session management (idle/absolute timeout, concurrent sessions) — see §10
- Email provider/sender configuration and email templates
- Prehooks
- Allowed Origins
- Redirect URIs
- JWT settings (token/refresh expiration, cookie settings)
- SSO configurations (roles/groups/domains/domain config)
- Entitlements: features, plans, plan-feature links, assignments, feature flags
- API access control routes
- **User groups** — definitions, group roles, group members (see §12)
- **User tenant memberships and their per-tenant roles** (requires the users to
  exist in the destination first — see §8)
- **User attributes** — lock state, phone number, metadata, vendor metadata,
  external ID, name (see §13)
- **Cutover-delta users** (opt-in, without passwords/MFA secrets — see §11)
- **Tenant API tokens** — recreated with **new** clientId/secret by default, or
  carried over with their **original** clientId/secret in preserve mode (see §9)
- **User API tokens** — recreated with **new** clientId/secret (see §9)

**Migrated by Frontegg Support (contact us):**
- Users' secret material: password hashes, MFA secrets/enrollment
  (user records themselves can be created by this tool at cutover — minus that
  secret material — see §11; Support's migration remains the lossless route)
- Not a migration, but a prerequisite for keeping tenant API token
  credentials: Support enables the `tenant-api-token-export-import-enabled`
  feature flag on both environments (see §9)

**Not migrated:**
- Builder settings
- Custom login, user pools
- Webhooks — they cannot be created through the API
- Flows
- Integrations (except email provider)
- Audit trail
- Early-access feature enrollment and other portal-internal settings (not
  exposed by the public API — ask Frontegg which EA flags are on per tenant)

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

**Tenant API tokens** (`MIGRATE_TENANT_API_TOKENS`) can be migrated two ways.
Choose with `TENANT_API_TOKENS_MODE` in `.env`:

| Mode | Credentials after migration | Requires | Writes to the source? |
|------|-----------------------------|----------|-----------------------|
| `recreate` (default) | **New** clientId + secret per token, written to a CSV. Every consumer must switch to them. | Nothing | Never |
| `preserve` | **Original** clientId + secret. Consumers keep their credentials. | Feature flag `tenant-api-token-export-import-enabled` on **both** environments, enabled by Frontegg Support | Different regions: never. Same region: each token is deleted from the source as it is moved, and only with `TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true` |

Any other value stops the step without changing anything.

**User API tokens** (`MIGRATE_USER_API_TOKENS`) are always recreated with new
credentials: there is no way to carry a user token's secret over.

Tokens whose expiry has already passed are skipped in both modes unless
`MIGRATE_EXPIRED_API_TOKENS=true`.

### Recreate mode (default): new credentials

Frontegg generates the `clientId` and `secret` server-side and returns the
secret exactly once, at creation, so each new token is written to a CSV the
moment it is created, alongside the source `clientId` it replaces:

| Flag | Output CSV |
|------|-----------|
| `MIGRATE_TENANT_API_TOKENS` (recreate mode) | `migrated_tenant_api_tokens.csv` |
| `MIGRATE_USER_API_TOKENS` | `migrated_user_api_tokens.csv` |

Both files are created with mode `0600` and are git-ignored. Treat them as
secrets: hand them over securely, then delete your local copy. If a file is lost,
the only recovery is deleting the tokens and creating them again.

**Tenant API tokens** keep their description, metadata, expiry and roles, with
role IDs remapped to the destination. Re-runs skip a token when the destination
tenant already has a token with the same description, when the source token's
clientId is already in the destination (carried over by preserve mode), or when
the CSV records a copy that still exists in the destination.

**User API tokens** carry no roles of their own — a user token inherits whatever
roles the user holds on that tenant — so run `MIGRATE_USER_TENANT_ROLES` first or
the new token will resolve to the wrong permissions.

**Cost.** Neither token type has a cross-tenant listing endpoint. Tenant tokens
cost one call per tenant. User tokens are worse: the list endpoint requires both
`frontegg-tenant-id` and `frontegg-user-id`, so discovery costs one call per
(user, tenant) membership. On a large account that is thousands of calls, which
is why `MIGRATE_USER_API_TOKENS` is off by default.

### Preserve mode: original credentials

> **Requires a Frontegg feature flag.** Preserve mode uses Frontegg's tenant API
> token export/import endpoints, which Frontegg enables per environment with the
> feature flag `tenant-api-token-export-import-enabled`. Contact Frontegg Support
> to enable it on **both** the source and the destination environment. It can
> take a few minutes to apply. If it is off on either side, the step stops with
> an error and changes nothing. It never falls back to recreating tokens.
>
> The destination must also allow API tokens and use **Client Credentials** as
> its machine-to-machine authentication strategy.

The export returns each token with its stored (hashed) secret, and the import
stores that hash unchanged, so the clientId + secret the customer already holds
authenticate against the destination. No secret is ever generated, printed, or
written to a CSV in this mode.

**Order.** Run it after tenants, roles and permissions exist in the destination
(`full-migration`). It is part of `post-user-migration` and `api-tokens-only`.

What a run does:

1. Exports every tenant API token from both environments.
2. Skips tokens that are already in the destination (same clientId, so re-runs
   are safe), expired tokens, **federated** tokens (their clientId is derived from
   the source environment, so they cannot work anywhere else), tokens whose
   tenant does not exist in the destination, and tokens the import would reject
   as malformed. Each skip is logged with its reason.
3. **Copies** the remaining tokens into the destination, 25 per call. The
   destination checks each token's tenant, roles and permissions; a token it
   rejects is logged with the reason and nothing is written for it.
4. **Moves** tokens that need it (same region only, below).

**Different regions** (e.g. EU → US): tokens are copied, and the source keeps
working until you delete its tokens. Delete them in the source once consumers
have switched to the destination.

**Same region** (`BASE_URL_1` equals `BASE_URL_2`): a clientId can exist only
once per region, so the destination rejects the copy (`409 ER-01244`) while the
source still holds the token. A token has to be **moved**: deleted from the
source, then imported into the destination. Recommended flow:

1. Run with `TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=false` (the default). Nothing
   is deleted. The run lists every token that needs a move. The destination
   rejects a clientId conflict only after the tenant, role and permission checks
   pass, so every listed token is known to be importable. Fix anything else the
   run reports.
2. Plan the cutover. Each moved token works **nowhere** for a few seconds
   between the delete and the import, and from then on works only against the
   destination.
3. Set `TENANT_API_TOKENS_ALLOW_SOURCE_DELETE=true` and re-run. For each token:
   - it is backed up to `TENANT_API_TOKENS_BACKUP_DIR` (mode `0600`,
     git-ignored, **contains the secret hash**),
   - deleted from the source,
   - imported into the destination (retried on transient errors),
   - and, if that import still fails, re-imported into the source unchanged.

   The orchestrator's preflight warns when source deletes are allowed. Delete
   the backups once the migration is confirmed.

**After migrating**, consumers keep their clientId and secret but must exchange
them at the **destination** environment's domain
(`POST https://<destination domain>/identity/resources/auth/v2/api-token`). The
JWTs they receive are signed with the destination's keys, so anything that
verifies them must use the destination's keys. Tokens already issued by the
source are not valid there.

**Good to know:**
- **Webhooks:** the import does **not** fire `frontegg.tenantApiToken.created`
  in the destination. Moving a token **does** fire
  `frontegg.tenantApiToken.deleted` in the source.
- **private_key_jwt:** public keys registered on a token are not carried over.
  The secret works in the destination, but re-register the public keys there
  before relying on `private_key_jwt`.
- **Roles and permissions** are matched by key. A role key matches account-level
  roles and roles on the token's own tenant. A key that matches more than one
  role is rejected (`409 ER-01246`). If a role lands at a different scope than
  in the source (account-level instead of tenant-scoped, or the reverse), the
  run warns.
- The import does not enforce the "API tokens require roles or permissions"
  setting, so tokens without roles carry over as they are.
- If the destination tenant already has a token with the same description but
  a different clientId (for example, one made by an earlier recreate-mode run),
  the run warns. Delete that copy once consumers use the original credentials.
- **Pacing:** the export is limited to 10 calls per minute and the import to 100
  per minute per environment. The step paces itself.

**Restoring a token from a backup.** If a move reports `ROLLBACK FAILED`, the
token exists in neither environment. Each backup holds the exact import body:

```bash
# Vendor token for the environment to restore into (source or destination)
TOKEN=$(curl -s -X POST "$BASE_URL/auth/vendor" -H 'Content-Type: application/json' \
  -d '{"clientId":"<client id>","secret":"<api key>"}' | jq -r .token)

jq '.importBody' tenant_api_token_backups/<clientId>-<time>.json | \
  curl -s -X POST "$BASE_URL/identity/resources/tenants/api-tokens/v1/import" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d @-
```

The response lists `imported: true` per token on success.

---

## 10. Auth & security policies

`MIGRATE_AUTH_POLICIES` migrates the policies under **Authentication** in the
portal, none of which any other step touches:

| Policy | What moves |
|--------|-----------|
| MFA policy | `enforceMFAType` (**forced MFA**), remember-device, device expiration |
| MFA methods | Authenticator app / SMS / email settings |
| Lockout policy | enabled, max attempts |
| Password | complexity rules, passphrases, pwned-password blocking |
| Password history | enabled, history size |
| Captcha | site/secret keys, min score, ignored emails (skipped when keys unset) |
| Sessions | idle timeout, absolute timeout, concurrent-session limit |

**Per-tenant overrides.** MFA, lockout, and session policies can be overridden
per tenant (this is how "forced MFA for tenant X" is configured). Frontegg has
no list-overrides endpoint, so the step reads each source tenant's *effective*
policy and treats "differs from the environment default" as an override, then
replays it onto the same tenant in the destination. Costs ~3 GETs per source
tenant; disable with `AUTH_POLICIES_TENANT_OVERRIDES=false`.

Idempotent: each policy is compared (server fields stripped) and written only
when it differs.

---

## 11. Cutover delta — users created after the export

Users import is a snapshot: anyone who signed up in the source **after**
Frontegg Support's export does not exist in the destination. Two ways to close
that gap at cutover:

1. **Lossless (recommended for password users):** ask Frontegg Support for a
   delta user migration, then re-run `post-user-migration`.
2. **Self-serve:** `MIGRATE_MISSING_USERS` (profile `cutover-delta`) creates
   every source-only user through the data-migration endpoint, carrying email,
   name, phone, metadata, external ID, provider, verified state, and the
   primary tenant's roles. **Passwords and MFA secrets cannot be read through
   the public API**, so these users arrive without them: they must reset their
   password on first login (SSO/passwordless users are unaffected). Created
   users are listed in `migrated_missing_users.csv`.

Either way, run `verify_migration.py` first — its **users** section lists
exactly who is missing, newest first (`--since <ISO date>` narrows it to users
created after your export date).

The remaining tenant memberships of a delta user are filled in by
`MIGRATE_USER_TENANT_ROLES`, which runs after it in the same profile.

---

## 12. User groups

`MIGRATE_GROUPS` migrates the environment groups configuration and, per
tenant: group definitions (name, description, color, metadata), group roles
(translated by role key), and group members (matched by email — users must
already exist in the destination; anyone missing is reported, never created).

Additive and idempotent: existing groups are topped up with missing roles and
members; nothing is ever removed. Groups managed outside Frontegg (e.g. SCIM)
are migrated but flagged, since their manager may recreate them.

---

## 13. User attributes

`MIGRATE_USER_ATTRIBUTES` syncs, for every user that exists on both sides
(matched by email):

- **Lock state** — users locked in the source are locked in the destination.
  Users locked *only* in the destination are left locked unless
  `USER_ATTRIBUTES_UNLOCK=true`.
- name, phone number, metadata, vendor metadata, external ID — written only
  when the source value is non-empty and differs; destination data is never
  blanked.
- profile picture URL — attempted, but the destination accepts it only when
  the URL's domain is on its allowed-domains list; rejected pictures are
  reported.

Reported but **not writable** via the public API: `mfaEnrolled`, `verified`,
`provider`, `managedBy` (like password hashes and MFA secrets, these move only
via Frontegg Support).

---

## 14. Verification (read-only)

```bash
python3 verify_migration.py                 # writes verify_report.json
python3 verify_migration.py --since 2026-08-01T00:00:00Z   # focus the user delta
python3 verify_migration.py --tokens --sso  # include per-tenant token/SSO checks (slow)
```

Compares the two accounts and reports every difference — both clients are
opened read-only, so it is safe at any time, including production cutover:

- **endpoint parity** — probes ~15 endpoints on both stacks and flags any
  that answer differently (catches region differences like `users/v2`
  relations availability)
- tenants, users (incl. the **cutover-delta list** of source-only users with
  creation dates), memberships & membership roles (by role key), roles /
  permissions / categories, groups, applications
- policies — MFA / lockout / password / captcha / sessions drift
- opt-in: per-tenant API tokens (`--tokens`; count and description parity in
  recreate mode, every source clientId present in the destination when
  `TENANT_API_TOKENS_MODE=preserve`), SSO configs (`--sso`)

Exit code 0 = no differences, 1 = differences found (CI-friendly). Full detail
lands in the JSON report; secrets are never written to it.

---

## 15. Troubleshooting

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
- **"Tenant API token export/import is not enabled"** (`403 ER-01247`) — the
  `tenant-api-token-export-import-enabled` feature flag is off on that
  environment. Ask Frontegg Support to enable it on both environments, then
  re-run. If it was just enabled, wait a few minutes. Nothing was changed.
- **"Api tokens are disabled"** (preserve mode) — API tokens are off in that
  environment, or the destination's machine-to-machine strategy is not Client
  Credentials.
- **Preserve mode lists tokens that "can only be MOVED"** — source and
  destination share a region. See [Preserve mode](#preserve-mode-original-credentials)
  for the move flow.
- **`ER-01245` / `ER-01246`** in preserve mode — a role or permission key is
  missing in the destination, or matches more than one role there. Run
  `MIGRATE_ROLES` / `MIGRATE_PERMISSIONS` first, or remove the duplicate key.

Questions or issues? Contact Frontegg Support.
