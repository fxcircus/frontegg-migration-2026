---
name: run-migration
description: Run a Frontegg account-to-account migration using the profile-driven orchestrator (migrate -> delete). Use when the user wants to migrate a Frontegg account's configuration, run the migration script/orchestrator, or delete data from a destination account.
---

# Run Frontegg Migration

Operational workflow for migrating one Frontegg account's configuration to
another via the orchestrator in `orchestrator/run.py`. Migrations write to a
live destination account and can delete data, so follow the steps and always
dry-run first.

## Key facts

- Always run from the project root with the project venv: `.venv/bin/python`.
- Credentials/secrets live in `.env` (never edit without explicit permission, never create new `.env` files).
- Behavior (what runs) is driven by **profiles** in `orchestrator/profiles.json` plus `--set` overrides — not by hand-editing `.env` flags.
- Source = `BASE_URL_1/CLIENT_ID_1/API_KEY_1`. Destination (where changes land) = `BASE_URL_2/CLIENT_ID_2/API_KEY_2`.
- This tool migrates account **configuration** only. User and group migration is handled separately by Frontegg Support.
- Migration runs log to `migration.log`; deletions log to a separate `delete.log` (both at the project root).

## Workflow

Copy this checklist and track progress:

```
- [ ] Step 1: Confirm direction (source -> destination) and pick a profile
- [ ] Step 2: Dry-run to review the resolved plan
- [ ] Step 3: Execute the run
- [ ] Step 4: Verify migration.log and destination account
```

### Step 1: Pick a profile

List available profiles:

```bash
.venv/bin/python orchestrator/run.py --list-profiles
```

| Profile | Use for |
|---------|---------|
| `full-migration` | All supported configuration end-to-end (no deletions) |
| `roles-only` | Categories + permissions + roles |
| `delete-cleanup` | DESTRUCTIVE: delete data from destination |

### Step 2: Dry-run (always)

Review the resolved migration/deletion steps and the destination URL
without making changes:

```bash
.venv/bin/python orchestrator/run.py --profile <profile> --dry-run
```

Confirm the **Destination** line points at the intended account before proceeding.

### Step 3: Execute

Non-destructive run (prompts for confirmation unless `--yes`):

```bash
.venv/bin/python orchestrator/run.py --profile <profile>
.venv/bin/python orchestrator/run.py --profile full-migration --yes
```

Override a single flag (repeatable):

```bash
.venv/bin/python orchestrator/run.py --profile roles-only --set MIGRATE_TENANTS=true
```

Destructive deletion (requires typing `DELETE`, or `--confirm-delete` non-interactively):

```bash
.venv/bin/python orchestrator/run.py --profile delete-cleanup --confirm-delete
```

### Step 4: Verify

- Tail `migration.log` (migrations) or `delete.log` (deletions) for `Error`/`Failed`/`✗` lines.
- Re-runs are safe: existing tenants/roles are skipped.

## Safety rules

- Never edit or create `.env` files.
- Always `--dry-run` and confirm the destination before a real run.
- `delete-cleanup` is irreversible against the destination — only run when explicitly requested.
- Don't disable the interactive confirmation for deletions unless the user asks.
