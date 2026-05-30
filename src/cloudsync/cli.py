"""
cloudsync.cli — Click-based CLI entry point.

Phase 1: validate, init, status
Phase 2: watch start, watch stop, watch status, watch logs
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from cloudsync.config import load_config, generate_template, ConfigError, CloudSyncConfig


# ── Shared helpers ───────────────────────────────────────────

def config_option(required: bool = True):
    return click.option(
        "--config", "-c",
        type=click.Path(exists=False),
        default="cloudsync.yaml",
        help="Path to cloudsync YAML config file",
        required=required,
    )


def _load_or_fail(config_path: str, check_paths: bool = True) -> CloudSyncConfig:
    try:
        return load_config(config_path, check_paths=check_paths)
    except FileNotFoundError as e:
        click.secho(f"Error: {e}", fg="red")
        sys.exit(1)
    except ConfigError as e:
        click.secho(f"Config validation failed ({len(e.errors)} errors):", fg="red")
        for err in e.errors:
            click.secho(f"  ✗ {err}", fg="yellow")
        sys.exit(1)


# ── Main CLI group ───────────────────────────────────────────

@click.group()
@click.version_option(version="0.1.0", prog_name="cloudsync")
def cli():
    """cloudsync — Config-driven file sync with fswatch + rclone + cron."""
    pass


# ── Phase 1: init, validate, status ─────────────────────────

@cli.command()
@click.option("--output", "-o", default="cloudsync.yaml", help="Output path for config file")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing file")
def init(output: str, force: bool):
    """Create a new cloudsync config file from template."""
    output_path = Path(output)
    if output_path.exists() and not force:
        click.secho(f"File already exists: {output_path}", fg="yellow")
        click.echo("Use --force to overwrite, or --output for different path")
        sys.exit(1)
    template = generate_template()
    output_path.write_text(template)
    click.secho(f"Created config: {output_path}", fg="green")
    click.echo(f"\nNext steps:")
    click.echo(f"  1. Edit {output_path} with your paths and remote details")
    click.echo(f"  2. Run: cloudsync validate --config {output_path}")
    click.echo(f"  3. Run: cloudsync status --config {output_path}")


@cli.command()
@config_option()
@click.option("--check-paths/--no-check-paths", default=True, help="Verify source dirs exist")
def validate(config: str, check_paths: bool):
    """Validate a cloudsync config file."""
    click.echo(f"Validating: {config}")
    cfg = _load_or_fail(config, check_paths=check_paths)

    click.secho("Config is valid!", fg="green")
    click.echo()
    click.echo(f"  Project:      {cfg.project.name}")
    click.echo(f"  Remote:       {cfg.remote.name} ({cfg.remote.type}/{cfg.remote.provider})")
    click.echo(f"  Bucket:       {cfg.remote.bucket}")
    click.echo(f"  Region:       {cfg.remote.region}")
    click.echo(f"  Directories:  {len(cfg.directories)}")
    click.echo(f"  Log dir:      {cfg.project.log_dir}")
    click.echo()

    click.echo("  Directories:")
    for d in cfg.directories:
        watch_icon = "👁" if d.watch else "—"
        excludes = f" (excludes: {len(d.exclude)})" if d.exclude else ""
        click.echo(f"    {watch_icon}  {d.name}")
        click.echo(f"       src:  {d.source}")
        click.echo(f"       dst:  {cfg.remote.bucket}/{d.dest}{excludes}")

    click.echo()
    click.echo("  Sync:")
    click.echo(f"    Transfers:  {cfg.sync.transfers}")
    click.echo(f"    Checkers:   {cfg.sync.checkers}")
    click.echo(f"    Checksum:   {cfg.sync.checksum}")
    click.echo(f"    Debounce:   {cfg.sync.debounce_seconds}s")

    click.echo()
    click.echo("  Schedule:")
    click.echo(f"    Real-time:  {cfg.schedule.realtime}")
    if cfg.schedule.weekly_full.enabled:
        click.echo(f"    Weekly:     {cfg.schedule.weekly_full.day} at {cfg.schedule.weekly_full.time}")
    if cfg.schedule.monthly_audit.enabled:
        click.echo(f"    Monthly:    day {cfg.schedule.monthly_audit.day} at {cfg.schedule.monthly_audit.time}")


@cli.command()
@config_option()
def status(config: str):
    """Show current sync status — tools, watchers, remotes."""
    cfg = _load_or_fail(config, check_paths=False)

    click.secho(f"cloudsync: {cfg.project.name}", fg="cyan", bold=True)
    click.echo()

    # Check external tools
    tools = {
        "rclone": shutil.which("rclone"),
        "fswatch": shutil.which("fswatch"),
        "crontab": shutil.which("crontab"),
    }

    click.echo("  External tools:")
    for tool, path in tools.items():
        if path:
            click.secho(f"    ✓ {tool}: {path}", fg="green")
        else:
            click.secho(f"    ✗ {tool}: not found", fg="red")
    click.echo()

    # Check rclone remote
    if tools["rclone"]:
        import subprocess
        result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
        remotes = [r.strip().rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
        if cfg.remote.name in remotes:
            click.secho(f"  rclone remote '{cfg.remote.name}': configured", fg="green")
        else:
            click.secho(f"  rclone remote '{cfg.remote.name}': NOT configured", fg="red")
            click.echo(f"    Run: rclone config  → create remote named '{cfg.remote.name}'")
    click.echo()

    # Check watcher status (Phase 2)
    if tools["fswatch"]:
        from cloudsync.watcher import get_status
        from cloudsync.logger import setup_logging
        setup_logging(cfg, console=False)

        statuses = get_status(cfg)
        if statuses:
            click.echo("  Watchers:")
            for name, meta in statuses.items():
                if meta.status == "running":
                    click.secho(f"    ✓ {name}: running (PID {meta.pid}, {meta.event_count} events)",
                                fg="green")
                elif meta.status == "dead":
                    click.secho(f"    ✗ {name}: dead (was PID {meta.pid})", fg="red")
                else:
                    click.secho(f"    — {name}: stopped", fg="yellow")
                    if meta.last_event_at:
                        click.echo(f"      Last event: {meta.last_event_at}")
    click.echo()

    # Check source directories
    click.echo("  Directories:")
    for d in cfg.directories:
        source_exists = Path(d.source).exists()
        if source_exists:
            import os
            file_count = sum(1 for _, _, files in os.walk(d.source) for _ in files)
            click.secho(f"    ✓ {d.name}: {d.source} ({file_count:,} files)", fg="green")
        else:
            click.secho(f"    ✗ {d.name}: {d.source} (not found)", fg="red")
    click.echo()

    # Check inotify limit
    inotify_path = Path("/proc/sys/fs/inotify/max_user_watches")
    if inotify_path.exists():
        limit = int(inotify_path.read_text().strip())
        if limit < 524288:
            click.secho(f"  ⚠ inotify limit: {limit:,} (recommend 2,097,152)", fg="yellow")
            click.echo("    Fix: echo 2097152 | sudo tee /proc/sys/fs/inotify/max_user_watches")
        else:
            click.secho(f"  inotify limit: {limit:,} (OK)", fg="green")


# ── Phase 2: watch commands ──────────────────────────────────

@cli.group()
def watch():
    """Manage fswatch file monitors."""
    pass


@watch.command("start")
@config_option()
@click.option("--dir", "-d", "directory", help="Start watcher for specific directory only")
@click.option("--daemon", is_flag=True, help="Run in background (for systemd/scripts)")
def watch_start(config: str, directory: str, daemon: bool):
    """Start fswatch for monitored directories."""
    cfg = _load_or_fail(config)

    # Check fswatch is installed
    if not shutil.which("fswatch"):
        click.secho("Error: fswatch not found. Install with: sudo apt install fswatch", fg="red")
        sys.exit(1)

    from cloudsync.watcher import start_watcher, start_all, get_watched_directories
    from cloudsync.logger import setup_logging
    log_dirs = setup_logging(cfg, console=not daemon)

    if directory:
        # Start single directory
        dir_configs = [d for d in cfg.directories if d.name == directory and d.watch]
        if not dir_configs:
            click.secho(f"Error: Directory '{directory}' not found or watch=false", fg="red")
            available = [d.name for d in get_watched_directories(cfg)]
            click.echo(f"  Available: {', '.join(available)}")
            sys.exit(1)

        try:
            meta = start_watcher(cfg, dir_configs[0])
            click.secho(f"Started watcher for '{directory}' (PID {meta.pid})", fg="green")
        except RuntimeError as e:
            click.secho(f"Error: {e}", fg="red")
            sys.exit(1)
    else:
        # Start all watched directories
        results = start_all(cfg)
        started = sum(1 for m in results.values() if m.status == "running")
        failed = sum(1 for m in results.values() if m.status == "error")

        for name, meta in results.items():
            if meta.status == "running":
                click.secho(f"  ✓ {name}: started (PID {meta.pid})", fg="green")
            else:
                click.secho(f"  ✗ {name}: failed to start", fg="red")

        click.echo()
        click.secho(f"Started {started}/{started + failed} watchers", fg="green" if not failed else "yellow")
        click.echo(f"Logs: {cfg.project.log_dir}/fswatch/")

    if not daemon:
        click.echo()
        click.echo("Watchers are running in background threads.")
        click.echo("Use 'cloudsync watch status' to check them.")
        click.echo("Use 'cloudsync watch stop' to stop them.")


@watch.command("stop")
@config_option()
@click.option("--dir", "-d", "directory", help="Stop watcher for specific directory only")
def watch_stop(config: str, directory: str):
    """Stop fswatch watchers."""
    cfg = _load_or_fail(config, check_paths=False)

    from cloudsync.watcher import stop_watcher, stop_all
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=True)

    if directory:
        stopped = stop_watcher(cfg, directory)
        if stopped:
            click.secho(f"Stopped watcher for '{directory}'", fg="green")
        else:
            click.secho(f"Watcher for '{directory}' was not running", fg="yellow")
    else:
        results = stop_all(cfg)
        for name, stopped in results.items():
            if stopped:
                click.secho(f"  ✓ {name}: stopped", fg="green")
            else:
                click.secho(f"  — {name}: was not running", fg="yellow")


@watch.command("status")
@config_option()
def watch_status(config: str):
    """Show status of all watchers."""
    cfg = _load_or_fail(config, check_paths=False)

    from cloudsync.watcher import get_status
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)

    statuses = get_status(cfg)

    if not statuses:
        click.echo("No watched directories configured.")
        return

    click.echo()
    for name, meta in statuses.items():
        # Status indicator
        if meta.status == "running":
            click.secho(f"  ● {name}", fg="green", nl=False)
            click.echo(f" — PID {meta.pid}")
        elif meta.status == "dead":
            click.secho(f"  ✗ {name}", fg="red", nl=False)
            click.echo(f" — process died (was PID {meta.pid})")
        else:
            click.secho(f"  ○ {name}", fg="yellow", nl=False)
            click.echo(f" — stopped")

        # Details
        click.echo(f"    Source:  {meta.source_path}")
        if meta.started_at:
            click.echo(f"    Started: {meta.started_at}")
        if meta.stopped_at:
            click.echo(f"    Stopped: {meta.stopped_at}")
        click.echo(f"    Events:  {meta.event_count}")
        if meta.last_event_at:
            click.echo(f"    Last:    {meta.last_event_at}")
            click.echo(f"    File:    {meta.last_event_path}")
        click.echo()


@watch.command("logs")
@config_option()
@click.option("--dir", "-d", "directory", required=True, help="Directory name to show logs for")
@click.option("--tail", "-n", default=20, help="Number of log lines to show")
@click.option("--date", help="Specific date (YYYY-MM-DD), default today")
def watch_logs(config: str, directory: str, tail: int, date: str):
    """Show recent fswatch events for a directory."""
    cfg = _load_or_fail(config, check_paths=False)

    from cloudsync.watcher import read_events
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)

    events = read_events(cfg, directory, tail=tail, date=date)

    if not events:
        click.echo(f"No events found for '{directory}'" +
                    (f" on {date}" if date else " today"))
        click.echo(f"Log dir: {cfg.project.log_dir}/fswatch/")
        return

    click.echo(f"Last {min(tail, len(events))} events for '{directory}':")
    click.echo()
    for line in events:
        # Color-code by event type
        if "Created" in line:
            click.secho(f"  + {line}", fg="green")
        elif "Removed" in line:
            click.secho(f"  - {line}", fg="red")
        elif "Updated" in line or "Modified" in line:
            click.secho(f"  ~ {line}", fg="yellow")
        elif "Renamed" in line or "Moved" in line:
            click.secho(f"  → {line}", fg="cyan")
        else:
            click.echo(f"  {line}")

    click.echo()
    click.echo(f"Total lines in log: {len(read_events(cfg, directory, tail=None, date=date))}")


# ── Phase 3+ placeholders ───────────────────────────────────

@cli.command()
@config_option()
@click.option("--dir", "-d", "directory", help="Specific directory to diff")
def diff(config: str, directory: str):
    """Show what would sync (dry run). (Phase 3)"""
    click.secho("Not yet implemented — Phase 3", fg="yellow")


@cli.command("sync")
@config_option()
@click.option("--dir", "-d", "directory", help="Specific directory to sync")
@click.option("--all", "sync_all", is_flag=True, help="Sync all directories")
@click.option("--full", is_flag=True, help="Force full sync ignoring fswatch")
@click.option("--dry-run", is_flag=True, help="Show what would happen")
def sync_cmd(config: str, directory: str, sync_all: bool, full: bool, dry_run: bool):
    """Sync changed files to S3. (Phase 3)"""
    click.secho("Not yet implemented — Phase 3", fg="yellow")


@cli.command()
@config_option()
@click.option("--dir", "-d", "directory", help="Specific directory to check")
@click.option("--checksum", is_flag=True, help="Use MD5 checksum comparison")
def check(config: str, directory: str, checksum: bool):
    """Verify source matches destination. (Phase 3)"""
    click.secho("Not yet implemented — Phase 3", fg="yellow")


@cli.command()
@config_option()
@click.option("--week", help="Specific week (e.g., 2026-W18)")
def changelog(config: str, week: str):
    """Show change history. (Phase 4)"""
    click.secho("Not yet implemented — Phase 4", fg="yellow")


@cli.group()
def schedule():
    """Manage cron jobs. (Phase 4)"""
    pass


@schedule.command("install")
@config_option()
def schedule_install(config: str):
    """Install cron jobs from config."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")


@schedule.command("remove")
@config_option()
def schedule_remove(config: str):
    """Remove all cloudsync cron jobs."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")


# ── Entry point ──────────────────────────────────────────────

def main():
    cli()


if __name__ == "__main__":
    main()
