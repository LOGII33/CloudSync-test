"""
cloudsync.cli — Click-based CLI entry point.

Phase 1: init, validate, status
Phase 2: watch start/stop/status/logs
Phase 3: sync, diff, check
Phase 4: schedule install/remove, up/down (placeholder)
"""
from __future__ import annotations
import shutil, sys
from pathlib import Path
import click
from cloudsync.config import load_config, generate_template, ConfigError, CloudSyncConfig


# ── Shared helpers ───────────────────────────────────────────

def config_option(required=True):
    return click.option("--config", "-c", type=click.Path(exists=False),
                         default="cloudsync.yaml", help="Path to config file", required=required)

def _load_or_fail(path, check_paths=True):
    try: return load_config(path, check_paths=check_paths)
    except FileNotFoundError as e: click.secho(f"Error: {e}", fg="red"); sys.exit(1)
    except ConfigError as e:
        click.secho(f"Validation failed ({len(e.errors)} errors):", fg="red")
        for err in e.errors: click.secho(f"  ✗ {err}", fg="yellow")
        sys.exit(1)

def _get_dir_config(cfg, name):
    for d in cfg.directories:
        if d.name == name: return d
    click.secho(f"Error: Directory '{name}' not found", fg="red")
    click.echo(f"  Available: {', '.join(d.name for d in cfg.directories)}")
    sys.exit(1)


# ── Main CLI ─────────────────────────────────────────────────

@click.group()
@click.version_option(version="0.1.0", prog_name="cloudsync")
def cli():
    """cloudsync — Config-driven file sync with fswatch + rclone + cron."""
    pass


# ── Phase 1: init, validate, status ─────────────────────────

@cli.command()
@click.option("--output", "-o", default="cloudsync.yaml")
@click.option("--force", "-f", is_flag=True)
def init(output, force):
    """Create a new config file from template."""
    p = Path(output)
    if p.exists() and not force:
        click.secho(f"File exists: {p}. Use --force to overwrite.", fg="yellow"); sys.exit(1)
    p.write_text(generate_template())
    click.secho(f"Created: {p}", fg="green")
    click.echo(f"  Next: edit {p}, then cloudsync validate --config {p}")


@cli.command()
@config_option()
@click.option("--check-paths/--no-check-paths", default=True)
def validate(config, check_paths):
    """Validate a config file and show summary."""
    click.echo(f"Validating: {config}")
    cfg = _load_or_fail(config, check_paths=check_paths)
    click.secho("Config is valid!", fg="green")
    click.echo()

    # Project + Remote
    click.echo(f"  Project:   {cfg.project.name}")
    click.echo(f"  Remote:    {cfg.remote.name} → {cfg.remote.bucket}")
    click.echo(f"  Log dir:   {cfg.project.log_dir}")
    click.echo()

    # Directories
    click.echo(f"  Directories ({len(cfg.directories)}):")
    for d in cfg.directories:
        w = "👁" if d.watch else "—"
        ex = f" (excludes: {len(d.exclude)})" if d.exclude else ""
        click.echo(f"    {w}  {d.name}: {d.source} → {d.dest}{ex}")
    click.echo()

    # Sync settings
    click.echo(f"  Sync: transfers={cfg.sync.transfers}, checkers={cfg.sync.checkers}, checksum={cfg.sync.checksum}")
    click.echo()

    # Schedule
    click.echo("  Schedule:")
    rt = cfg.schedule.realtime
    ss = cfg.schedule.sync_schedule
    ia = cfg.schedule.integrity_audit

    if rt.enabled:
        click.secho(f"    ● Realtime:  every {rt.interval_minutes} min (smart sync)", fg="green")
    else:
        click.echo(f"    ○ Realtime:  disabled")

    if ss.enabled:
        if ss.frequency == "custom":
            click.secho(f"    ● Sync:      custom cron: {ss.cron} (method: {ss.method})", fg="green")
        else:
            day_str = f" {ss.day}" if ss.frequency == "weekly" else ""
            click.secho(f"    ● Sync:      {ss.frequency}{day_str} at {ss.time} (method: {ss.method}, scan_days={ss.scan_days})", fg="green")
    else:
        click.echo(f"    ○ Sync:      disabled")

    if ia.enabled:
        check_mode = "checksum" if ia.use_checksum else "size-only"
        click.secho(f"    ● Audit:     {ia.frequency} day {ia.day} at {ia.time} ({check_mode})", fg="green")
    else:
        click.echo(f"    ○ Audit:     disabled")


@cli.command()
@config_option()
def status(config):
    """Show current status — tools, watchers, schedules."""
    cfg = _load_or_fail(config, check_paths=False)
    click.secho(f"cloudsync: {cfg.project.name}", fg="cyan", bold=True)
    click.echo()

    # Tools
    tools = {"rclone": shutil.which("rclone"), "fswatch": shutil.which("fswatch"), "crontab": shutil.which("crontab")}
    click.echo("  Tools:")
    for t, p in tools.items():
        if p: click.secho(f"    ✓ {t}", fg="green")
        else: click.secho(f"    ✗ {t}: not found", fg="red")
    click.echo()

    # Remote
    if tools["rclone"]:
        import subprocess
        r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
        remotes = [x.strip().rstrip(":") for x in r.stdout.strip().split("\n") if x.strip()]
        if cfg.remote.name in remotes:
            click.secho(f"  Remote '{cfg.remote.name}': connected", fg="green")
        else:
            click.secho(f"  Remote '{cfg.remote.name}': NOT configured", fg="red")
    click.echo()

    # Watchers
    if tools["fswatch"]:
        from cloudsync.watcher import get_status
        from cloudsync.logger import setup_logging
        setup_logging(cfg, console=False)
        click.echo("  Watchers:")
        for name, meta in get_status(cfg).items():
            if meta.status == "running":
                click.secho(f"    ● {name}: PID {meta.pid}, {meta.event_count} events ({meta.monitor})", fg="green")
            elif meta.status == "dead":
                click.secho(f"    ✗ {name}: dead", fg="red")
            else:
                click.secho(f"    ○ {name}: stopped", fg="yellow")
    click.echo()

    # Schedule summary
    rt = cfg.schedule.realtime
    ss = cfg.schedule.sync_schedule
    ia = cfg.schedule.integrity_audit
    click.echo("  Schedules:")
    if rt.enabled:
        click.secho(f"    ● Realtime:  every {rt.interval_minutes} min → smart_sync(scan_days=1)", fg="green")
    else:
        click.echo(f"    ○ Realtime:  off")
    if ss.enabled:
        click.secho(f"    ● Sync:      {ss.frequency} → {ss.method}_sync(scan_days={ss.scan_days}, dir_scan={ss.include_dir_scan})", fg="green")
    else:
        click.echo(f"    ○ Sync:      off")
    if ia.enabled:
        click.secho(f"    ● Audit:     {ia.frequency} → full_integrity_check(checksum={ia.use_checksum})", fg="green")
    else:
        click.echo(f"    ○ Audit:     off")


# ── Phase 2: watch ───────────────────────────────────────────

@cli.group()
def watch():
    """Manage fswatch file monitors."""
    pass

@watch.command("start")
@config_option()
@click.option("--dir", "-d", "directory")
@click.option("--daemon", is_flag=True)
def watch_start(config, directory, daemon):
    """Start fswatch for monitored directories."""
    cfg = _load_or_fail(config)
    if not shutil.which("fswatch"):
        click.secho("Error: fswatch not found", fg="red"); sys.exit(1)
    from cloudsync.watcher import start_watcher, start_all, get_watched_directories
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=not daemon)

    if directory:
        dc = _get_dir_config(cfg, directory)
        if not dc.watch: click.secho(f"'{directory}' has watch: false", fg="red"); sys.exit(1)
        try:
            meta = start_watcher(cfg, dc)
            click.secho(f"Started '{directory}' (PID {meta.pid}, {meta.monitor})", fg="green")
        except RuntimeError as e: click.secho(f"Error: {e}", fg="red"); sys.exit(1)
    else:
        for name, meta in start_all(cfg).items():
            if meta.status == "running": click.secho(f"  ✓ {name}: PID {meta.pid} ({meta.monitor})", fg="green")
            else: click.secho(f"  ✗ {name}: failed", fg="red")

@watch.command("stop")
@config_option()
@click.option("--dir", "-d", "directory")
def watch_stop(config, directory):
    """Stop fswatch watchers."""
    cfg = _load_or_fail(config, check_paths=False)
    from cloudsync.watcher import stop_watcher, stop_all
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=True)
    if directory:
        if stop_watcher(cfg, directory): click.secho(f"Stopped '{directory}'", fg="green")
        else: click.secho(f"'{directory}' was not running", fg="yellow")
    else:
        for name, stopped in stop_all(cfg).items():
            if stopped: click.secho(f"  ✓ {name}: stopped", fg="green")
            else: click.secho(f"  — {name}: not running", fg="yellow")

@watch.command("status")
@config_option()
def watch_status(config):
    """Show watcher status."""
    cfg = _load_or_fail(config, check_paths=False)
    from cloudsync.watcher import get_status
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)
    for name, meta in get_status(cfg).items():
        if meta.status == "running":
            click.secho(f"  ● {name}: PID {meta.pid}, {meta.event_count} events ({meta.monitor})", fg="green")
        elif meta.status == "dead": click.secho(f"  ✗ {name}: dead", fg="red")
        else: click.secho(f"  ○ {name}: stopped", fg="yellow")
        if meta.last_event_at: click.echo(f"    Last: {meta.last_event_at} → {meta.last_event_path}")

@watch.command("logs")
@config_option()
@click.option("--dir", "-d", "directory", required=True)
@click.option("--tail", "-n", default=20)
@click.option("--date")
def watch_logs(config, directory, tail, date):
    """Show recent fswatch events."""
    cfg = _load_or_fail(config, check_paths=False)
    from cloudsync.watcher import read_events
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)
    events = read_events(cfg, directory, tail=tail, date=date)
    if not events: click.echo(f"No events for '{directory}'"); return
    for line in events:
        if "Created" in line: click.secho(f"  + {line}", fg="green")
        elif "Removed" in line: click.secho(f"  - {line}", fg="red")
        elif "Updated" in line or "Modified" in line: click.secho(f"  ~ {line}", fg="yellow")
        else: click.echo(f"  {line}")


# ── Phase 3: sync, diff, check ──────────────────────────────

@cli.command("sync")
@config_option()
@click.option("--dir", "-d", "directory", help="Sync specific directory")
@click.option("--all", "sync_all_flag", is_flag=True, help="Sync all directories")
@click.option("--full", is_flag=True, help="Full rclone scan (expensive, last resort)")
@click.option("--weekly", is_flag=True, help="Weekly: 8 days of logs + dir scan + report")
@click.option("--daily", is_flag=True, help="Daily: 2 days of logs")
@click.option("--dry-run", is_flag=True, help="Simulate without transferring")
@click.option("--scan-days", type=int, help="Custom: read N days of fswatch logs")
def sync_cmd(config, directory, sync_all_flag, full, weekly, daily, dry_run, scan_days):
    """Sync changed files to S3.

    \b
    Modes (pick one):
      (default)    Today's fswatch logs → size+mtime → checksum → sync
      --daily      2 days of logs → size+mtime → checksum → sync
      --weekly     8 days of logs + dir scan → size+mtime → checksum → sync
      --full       Direct rclone scan, no fswatch (expensive, last resort)
      --scan-days  Custom: read N days of logs
    """
    cfg = _load_or_fail(config)
    from cloudsync.syncer import sync_directory, sync_all as _sync_all, save_sync_log
    from cloudsync.logger import setup_logging
    setup_logging(cfg)

    # Determine mode and parameters
    if full:
        mode = "full"
        kwargs = {}
    else:
        mode = "smart"
        if scan_days:
            kwargs = {"scan_days": scan_days, "include_dir_scan": scan_days >= 7, "generate_report": scan_days >= 7}
        elif weekly:
            kwargs = {"scan_days": 8, "include_dir_scan": True, "generate_report": True}
        elif daily:
            kwargs = {"scan_days": 2, "include_dir_scan": False, "generate_report": False}
        else:
            kwargs = {"scan_days": 1, "include_dir_scan": False, "generate_report": False}

    if not directory and not sync_all_flag:
        click.secho("Specify --dir NAME or --all", fg="red")
        click.echo("  cloudsync sync --dir AIML --config tamizh.yaml")
        click.echo("  cloudsync sync --all --weekly --config tamizh.yaml")
        sys.exit(1)

    if directory:
        result = sync_directory(cfg, directory, mode=mode, dry_run=dry_run, **kwargs)
        _print_sync_result(result)
        save_sync_log(cfg, result)
    else:
        results = _sync_all(cfg, mode=mode, dry_run=dry_run, **kwargs)
        for name, result in results.items():
            _print_sync_result(result)
            save_sync_log(cfg, result)
        click.echo()
        total = sum(r.files_synced for r in results.values())
        click.secho(f"Total: {total} files synced across {len(results)} directories", fg="cyan")


def _print_sync_result(result):
    """Pretty-print a sync result with the full funnel."""
    color = {"success": "green", "dry_run": "cyan", "skipped": "yellow",
             "partial": "yellow", "failed": "red"}.get(result.status, "white")
    click.secho(f"\n  {result.dir_name}: {result.status}", fg=color, bold=True)
    if result.status == "skipped":
        click.echo("    No changes to sync"); return

    click.echo("    Pipeline:")
    if result.files_from_fswatch: click.echo(f"      fswatch logs:      {result.files_from_fswatch} files")
    if result.files_from_scan:
        click.echo(f"      directory scan:    {result.files_from_scan} files")
        if result.fswatch_coverage: click.echo(f"      fswatch coverage:  {result.fswatch_coverage}")
    if result.files_merged: click.echo(f"      merged:            {result.files_merged} files")
    if result.files_after_size_mtime or result.files_new:
        click.echo(f"      size+mtime:        {result.files_after_size_mtime} changed, {result.files_new} new")
    if result.files_after_checksum: click.echo(f"      checksum:          {result.files_after_checksum} confirmed")
    click.echo(f"      synced:            {result.files_synced} files")
    if result.duration_seconds: click.echo(f"    Duration: {result.duration_seconds:.1f}s")
    for e in result.errors: click.secho(f"    Error: {e}", fg="red")


@cli.command()
@config_option()
@click.option("--dir", "-d", "directory", required=True)
@click.option("--weekly", is_flag=True, help="Preview weekly sync (8 days + dir scan)")
@click.option("--scan-days", type=int, help="Custom: preview N days")
def diff(config, directory, weekly, scan_days):
    """Preview what would sync (no transfer).

    \b
    Shows the full funnel:
      fswatch detected → size+mtime filter → checksum → would sync
    """
    cfg = _load_or_fail(config)
    from cloudsync.syncer import diff_directory
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)

    days = scan_days or (8 if weekly else 1)
    scan = weekly or (scan_days and scan_days >= 7)
    label = f"weekly ({days} days + scan)" if scan else f"{days} day(s)"

    click.echo(f"Diff for '{directory}' ({label}):")
    result = diff_directory(cfg, directory, scan_days=days, include_dir_scan=scan)

    if "error" in result: click.secho(f"  Error: {result['error']}", fg="red"); sys.exit(1)
    if result["would_sync"] == 0: click.secho("  No changes to sync", fg="green"); return

    click.echo()
    click.echo(f"  fswatch detected:    {result['files_from_fswatch']} files")
    if result.get("files_from_scan"):
        click.echo(f"  directory scan:      {result['files_from_scan']} files")
        click.echo(f"  fswatch missed:      {result.get('fswatch_missed', 0)} files")
    click.echo(f"  merged candidates:   {result['merged']} files")
    click.echo(f"  size+mtime changed:  {result['size_mtime_changed']} files")
    click.echo(f"  checksum differs:    {result['checksum_differs']} files")
    click.echo(f"  new files:           {result['new_files']} files")
    click.echo()
    click.secho(f"  Would sync: {result['would_sync']} files", fg="cyan", bold=True)
    click.echo(f"\n  Run: cloudsync sync --dir {directory} --config {config}")


@cli.command()
@config_option()
@click.option("--dir", "-d", "directory", required=True)
@click.option("--checksum", is_flag=True, help="Use MD5 (slower, more accurate)")
def check(config, directory, checksum):
    """Verify source matches S3 destination.

    \b
    Compares ALL files (not just fswatch changes).
    Default: size-only (fast). --checksum: MD5 hash (thorough).
    """
    cfg = _load_or_fail(config)
    from cloudsync.hasher import full_integrity_check
    from cloudsync.logger import setup_logging
    setup_logging(cfg, console=False)

    dc = _get_dir_config(cfg, directory)
    mode = "checksum" if checksum else "size-only"
    click.echo(f"Checking '{directory}' ({mode})...")

    differs, missing, errors = full_integrity_check(cfg, dc, use_checksum=checksum)
    if not differs and not missing and not errors:
        click.secho(f"  ✓ All files match!", fg="green")
    else:
        if differs:
            click.secho(f"  ✗ {len(differs)} files differ", fg="red")
            for f in differs[:10]: click.echo(f"    {f}")
            if len(differs) > 10: click.echo(f"    ... and {len(differs)-10} more")
        if missing:
            click.secho(f"  ✗ {len(missing)} files missing from S3", fg="red")
            for f in missing[:10]: click.echo(f"    {f}")
            if len(missing) > 10: click.echo(f"    ... and {len(missing)-10} more")
        if errors:
            click.secho(f"  ⚠ {len(errors)} errors", fg="yellow")
        click.echo(f"\n  Fix: cloudsync sync --dir {directory} --config {config}")


# ── Phase 4: schedule + up/down (placeholder) ────────────────

@cli.group()
def schedule():
    """Manage cron jobs."""
    pass

@schedule.command("install")
@config_option()
def schedule_install(config):
    """Install cron jobs from config schedule."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")
    cfg = _load_or_fail(config, check_paths=False)
    click.echo("\n  Would install these cron jobs:")
    rt = cfg.schedule.realtime
    ss = cfg.schedule.sync_schedule
    ia = cfg.schedule.integrity_audit
    if rt.enabled:
        click.echo(f"    */{rt.interval_minutes} * * * * cloudsync sync --all --config {config}")
    if ss.enabled:
        if ss.frequency == "daily":
            h, m = ss.time.split(":")
            click.echo(f"    {m} {h} * * * cloudsync sync --all --daily --config {config}")
        elif ss.frequency == "weekly":
            h, m = ss.time.split(":")
            day_num = {"sunday":"0","monday":"1","tuesday":"2","wednesday":"3","thursday":"4","friday":"5","saturday":"6"}
            d = day_num.get(ss.day.lower(), "0")
            click.echo(f"    {m} {h} * * {d} cloudsync sync --all --weekly --config {config}")
        elif ss.frequency == "custom":
            click.echo(f"    {ss.cron} cloudsync sync --all --scan-days {ss.scan_days} --config {config}")
    if ia.enabled:
        h, m = ia.time.split(":")
        click.echo(f"    {m} {h} {ia.day} * * cloudsync check --dir ALL --config {config}")

@schedule.command("remove")
@config_option()
def schedule_remove(config):
    """Remove all cloudsync cron jobs."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")

@cli.command()
@config_option()
def up(config):
    """Start everything: watchers + cron + initial sync."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")

@cli.command()
@config_option()
def down(config):
    """Stop everything: watchers + cron."""
    click.secho("Not yet implemented — Phase 4", fg="yellow")


# ── Entry point ──────────────────────────────────────────────

def main(): cli()
if __name__ == "__main__": main()
