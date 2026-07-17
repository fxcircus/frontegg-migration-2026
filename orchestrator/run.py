"""Profile-driven migration orchestrator.

Runs the migration pipeline from a single command:
  1. migration (main.py) honoring the resolved MIGRATE_* flags
  2. (optional) destructive deletion (delete_account_data.py)

Behavior toggles come from a named profile (orchestrator/profiles.json) plus
optional --set overrides. Credentials/secrets stay in .env.

Examples:
  python3 orchestrator/run.py --list-profiles
  python3 orchestrator/run.py --profile full-migration --dry-run
  python3 orchestrator/run.py --profile full-migration
  python3 orchestrator/run.py --profile roles-only --set MIGRATE_TENANTS=true
  python3 orchestrator/run.py --profile delete-cleanup --confirm-delete
"""

import argparse
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from orchestrator import config as cfg

console = Console()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="orchestrator.run",
        description="Profile-driven Frontegg migration orchestrator (migrate -> delete).",
    )
    parser.add_argument("--profile", help="Name of the profile to run (see --list-profiles).")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        metavar="FLAG=VALUE",
        help="Override a single flag, e.g. --set MIGRATE_ROLES=true (repeatable).",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List available profiles and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the resolved plan (preflight) and exit without executing anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (non-destructive steps).",
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Required to run deletion steps non-interactively.",
    )
    return parser


def cmd_list_profiles():
    profiles = cfg.load_profiles()
    table = Table(title="Available Profiles", header_style="bold cyan")
    table.add_column("Profile", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    for name in sorted(profiles.keys()):
        profile = profiles[name]
        table.add_row(name, profile.get("description", ""))
    console.print(table)


def render_preflight(config):
    title = f"Run Plan: [bold]{config['profile'] or 'custom'}[/bold]"
    if config["description"]:
        title += f"\n[dim]{config['description']}[/dim]"
    console.print(Panel(title, expand=False))

    # Destination (where changes land) for visibility.
    dest = os.getenv("BASE_URL_2", "(unset)")
    source = os.getenv("BASE_URL_1", "(unset)")
    console.print(f"[dim]Source:[/dim] {source}    [dim]Destination:[/dim] {dest}")

    migrate_on = cfg.enabled_migration_steps(config)
    console.print(f"\n[bold]Migration steps ({len(migrate_on)}):[/bold]")
    if migrate_on:
        for flag in migrate_on:
            console.print(f"  [green]✓[/green] {flag}")
    else:
        console.print("  [dim](none)[/dim]")

    delete_on = cfg.enabled_deletion_steps(config)
    if delete_on:
        console.print(f"\n[bold red]Deletion steps ({len(delete_on)}) — DESTRUCTIVE:[/bold red]")
        for flag in delete_on:
            console.print(f"  [red]✗[/red] {flag}")


def confirm(config, args):
    """Return True if the run is approved."""
    delete_on = cfg.has_deletions(config)
    interactive = sys.stdin.isatty()

    if delete_on:
        console.print(
            f"\n[bold red]WARNING:[/bold red] this run will DELETE data from "
            f"{os.getenv('BASE_URL_2', '(unset)')}."
        )
        if not interactive:
            if not args.confirm_delete:
                console.print(
                    "[red]Refusing to delete non-interactively without --confirm-delete.[/red]"
                )
                return False
            return True
        answer = console.input("Type [bold]DELETE[/bold] to proceed: ").strip()
        return answer == "DELETE"

    if args.yes or not interactive:
        return True
    answer = console.input("\nProceed with this run? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def run_migration(config):
    """Run the migration if any migration step is enabled."""
    if not cfg.enabled_migration_steps(config):
        console.print("[dim]No migration steps enabled; skipping migration.[/dim]")
        return True
    import main as migration_main
    migration_main.main()
    return True


def run_deletions(config):
    """Run destructive deletions if any deletion step is enabled."""
    if not cfg.has_deletions(config):
        return True
    import delete_account_data
    delete_account_data.main()
    return True


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # All relative paths in the existing scripts assume the project root as cwd.
    os.chdir(PROJECT_ROOT)

    # Load credentials/secrets from .env (does not override flags we set later,
    # since load_dotenv defaults to override=False).
    load_dotenv()

    if args.list_profiles:
        cmd_list_profiles()
        return 0

    if not args.profile and not args.overrides:
        parser.error("Provide --profile (and/or --set overrides), or use --list-profiles.")

    try:
        config = cfg.resolve_config(
            profile_name=args.profile,
            overrides=args.overrides,
        )
    except cfg.ConfigError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        return 2

    render_preflight(config)

    if args.dry_run:
        console.print("\n[dim]Dry run — no changes made.[/dim]")
        return 0

    if not confirm(config, args):
        console.print("[yellow]Aborted by user.[/yellow]")
        return 1

    # Apply resolved flags to the environment BEFORE importing the scripts that
    # read them at import time.
    cfg.apply_to_env(config)

    start = time.time()

    run_migration(config)
    run_deletions(config)

    elapsed = time.time() - start
    console.print(f"\n[bold green]✓ Pipeline finished in {elapsed:.1f}s[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
