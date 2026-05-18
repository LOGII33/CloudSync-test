"""
cloudsync.cli — Click-based CLI entry point.

Phase 1 commands:
  cloudsync validate --config path/to/config.yaml
  cloudsync init --output path/to/config.yaml
  cloudsync status --config path/to/config.yaml

Later phases will add:
  cloudsync watch start/stop/logs
  cloudsync diff / sync / check
  cloudsync changelog
  cloudsync schedule install/remove
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cloudsync.config import load_config, generate_template, ConfigError, CloudSyncConfig


# ── Shared options ───────────────────────────────────────────

def config_option(required: bool = True):
    """Reusable --config option for commands that need a config file."""
    return click.option(
        "--config", "-c",
        type=click.Path(exists=False),
        default="cloudsync.yaml",
        help="Path to cloudsync YAML config file",
        required=required,
    )


def _load_or_fail(config_path: str, check_paths: bool = True) -> CloudSyncConfig:
    """Load config and handle errors with clean CLI output."""
    try:
        config = load_config(config_path, check_paths=check_paths)
        return config
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


# ── Phase 1 commands ─────────────────────────────────────────

@cli.command()
@click.option("--output", "-o", default="cloudsync.yaml", help="Output path for the config file")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing file")
def init(output: str, force: bool):
    """Create a new cloudsync config file from template."""
    output_path = Path(output)

    if output_path.exists() and not force:
        click.secho(f"File already exists: {output_path}", fg="yellow")
        click.echo("Use --force to overwrite, or choose a different path with --output")
        sys.exit(1)

    template = generate_template()
    output_path.write_text(template)
    click.secho(f"Created config: {output_path}", fg="green")
    click.echo("\nNext steps:")
    click.echo(f"  1. Edit {output_path} with your source/dest paths")
    click.echo(f"  2. Run: cloudsync validate --config {output_path}")
    click.echo(f"  3. Run: cloudsync status --config {output_path}")


@cli.command()
@config_option()
@click.option("--check-paths/--no-check-paths", default=True, help="Verify source directories exist")
def validate(config: str, check_paths: bool):
    """Validate a cloudsync config file."""
    click.echo(f"Validating: {config}")

    cfg = _load_or_fail(config, check_paths=check_paths)

    click.secho("Config is valid!", fg="green")
    click.echo()

    # Summary
    click.echo(f"  Project:      {cfg.project.name}")
    if cfg.remote.existing:
        click.echo(f"  Remote:       {cfg.remote.name} (Mode A — existing rclone remote)")
        click.echo(f"  Bucket:       {cfg.remote.bucket}  (path prefix / logical target in YAML)")
        click.echo("  Type/region:  (read from rclone config, not YAML)")
    else:
        click.echo(f"  Remote:       {cfg.remote.name} (Mode B — {cfg.remote.type}/{cfg.remote.provider})")
        click.echo(f"  Bucket:       {cfg.remote.bucket}")
        click.echo(f"  Region:       {cfg.remote.region}")
    click.echo(f"  Directories:  {len(cfg.directories)}")
    click.echo(f"  Log dir:      {cfg.project.log_dir}")
    click.echo()

    # Directory table
    click.echo("  Directories:")
    for d in cfg.directories:
        watch_icon = "👁" if d.watch else "—"
        excludes = f" (excludes: {len(d.exclude)})" if d.exclude else ""
        click.echo(f"    {watch_icon}  {d.name}")
        click.echo(f"       src:  {d.source}")
        click.echo(f"       dst:  {cfg.remote.bucket}/{d.dest}{excludes}")

    click.echo()

    # Sync settings
    click.echo("  Sync:")
    click.echo(f"    Transfers:  {cfg.sync.transfers}")
    click.echo(f"    Checkers:   {cfg.sync.checkers}")
    click.echo(f"    Checksum:   {cfg.sync.checksum}")
    click.echo(f"    Debounce:   {cfg.sync.debounce_seconds}s")

    click.echo()

    # Schedule
    click.echo("  Schedule:")
    click.echo(f"    Real-time:  {cfg.schedule.realtime}")
    if cfg.schedule.weekly_full.enabled:
        click.echo(f"    Weekly:     {cfg.schedule.weekly_full.day} at {cfg.schedule.weekly_full.time}")
    if cfg.schedule.monthly_audit.enabled:
        click.echo(f"    Monthly:    day {cfg.schedule.monthly_audit.day} at {cfg.schedule.monthly_audit.time}")


@cli.command()
@config_option()
def status(config: str):
    """Show current sync status — watchers, last sync, pending changes."""
    cfg = _load_or_fail(config, check_paths=False)

    click.secho(f"cloudsync: {cfg.project.name}", fg="cyan", bold=True)
    click.echo()

    # Check external tools
    import shutil
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

    # Check rclone remote exists
    if tools["rclone"]:
        import subprocess
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True, text=True, timeout=10,
        )
        remotes = [r.strip().rstrip(":") for r in result.stdout.strip().split("\n") if r.strip()]
        if cfg.remote.name in remotes:
            click.secho(f"  rclone remote '{cfg.remote.name}': configured", fg="green")
        else:
            click.secho(f"  rclone remote '{cfg.remote.name}': NOT configured", fg="red")
            click.echo(f"    Available remotes: {', '.join(remotes) if remotes else '(none)'}")
            click.echo(f"    Run: rclone config  →  create remote named '{cfg.remote.name}'")
    else:
        click.secho("  rclone: not found on PATH — skipping remote checks", fg="yellow")

    click.echo()
    if cfg.remote.existing:
        click.echo("  Remote mode:  Using existing rclone remote (YAML has name + bucket only)")
        click.echo("                Keys and backend type live in ~/.config/rclone/rclone.conf")
    else:
        click.echo("  Remote mode:  Full remote spec in YAML (future: cloudsync setup / rclone config create)")
        click.echo(f"                Would target type={cfg.remote.type}, region={cfg.remote.region}")

    click.echo()

    # Check source directories
    click.echo("  Directories:")
    for d in cfg.directories:
        source_exists = Path(d.source).exists()
        if source_exists:
            # Count files
            import os
            file_count = sum(1 for _, _, files in os.walk(d.source) for _ in files)
            click.secho(f"    ✓ {d.name}: {d.source} ({file_count:,} files)", fg="green")
        else:
            click.secho(f"    ✗ {d.name}: {d.source} (not found)", fg="red")

    click.echo()

    # Check log directory
    log_path = Path(cfg.project.log_dir)
    if log_path.exists():
        click.secho(f"  Log dir: {cfg.project.log_dir} (exists)", fg="green")
    else:
        click.secho(f"  Log dir: {cfg.project.log_dir} (will be created on first run)", fg="yellow")

    click.echo()

    # Check inotify limit (Linux only)
    inotify_path = Path("/proc/sys/fs/inotify/max_user_watches")
    if inotify_path.exists():
        limit = int(inotify_path.read_text().strip())
        watched_dirs = sum(1 for d in cfg.directories if d.watch)
        if limit < 524288:
            click.secho(
                f"  ⚠ inotify watch limit: {limit:,} (recommend 2,097,152); "
                f"{watched_dirs} dir(s) marked watch in config",
                fg="yellow",
            )
            click.echo("    Fix: echo 2097152 | sudo tee /proc/sys/fs/inotify/max_user_watches")
        else:
            click.secho(f"  inotify watch limit: {limit:,} (OK)", fg="green")


# ── Placeholder commands for later phases ────────────────────

@cli.group()
def watch():
    """Manage fswatch file monitors. (Phase 2)"""
    pass


@watch.command("start")
@config_option()
def watch_start(config: str):
    """Start fswatch for all monitored directories."""
    click.secho("Not yet implemented — Phase 2", fg="yellow")


@watch.command("stop")
@config_option()
def watch_stop(config: str):
    """Stop all fswatch processes."""
    click.secho("Not yet implemented — Phase 2", fg="yellow")


@watch.command("logs")
@config_option()
@click.option("--dir", "-d", "directory", help="Directory name to show logs for")
@click.option("--tail", "-n", default=20, help="Number of log lines")
def watch_logs(config: str, directory: str, tail: int):
    """Show recent fswatch events."""
    click.secho("Not yet implemented — Phase 2", fg="yellow")


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
@click.option("--week", help="Specific week to show (e.g., 2026-W18)")
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