"""Click CLI — entry point for all commands.

Implement in phases:

- Phase 1: ``init``, ``validate`` (wire to ``config.load`` / ``config.validate``).
- Phase 2: ``watch`` (start, stop, logs) via ``watcher``.
- Phase 3: ``diff``, ``sync``, ``check`` via ``hasher`` + ``syncer``.
- Phase 4: ``changelog``, ``schedule`` (install, remove) via ``scheduler`` + ``changelog``.

Intended commands (from architecture spec)::

    cloudsync init                    # --config path: interactive config
    cloudsync validate                # --config: check YAML
    cloudsync status
    cloudsync watch start             # --daemon
    cloudsync watch stop
    cloudsync watch logs              # --dir, --tail
    cloudsync diff                    # --dir: dry run
    cloudsync sync                    # --dir or --all; --full
    cloudsync check                   # --checksum --dir
    cloudsync changelog               # --week
    cloudsync schedule install
    cloudsync schedule remove

Use ``logger`` for structured logging across commands.
"""

from __future__ import annotations

import click


@click.group()
@click.version_option()
def cli() -> None:
    """Config-driven file sync (fswatch, rclone, cron)."""


@cli.command("init")
@click.option("--config", "config_path", type=click.Path(), default="cloudsync.yaml")
def init_cmd(config_path: str) -> None:
    """Create a new config file interactively."""
    raise NotImplementedError("Phase 1: implement interactive config creation")


@cli.command("validate")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True))
def validate_cmd(config_path: str) -> None:
    """Check config file for errors."""
    raise NotImplementedError("Phase 1: implement YAML load + validation in config.py")


@cli.command("status")
def status_cmd() -> None:
    """Show watcher status, last sync, pending changes."""
    raise NotImplementedError("Phase 2+: implement status aggregation")


@cli.group("watch")
def watch_group() -> None:
    """Control fswatch-backed directory monitors."""


@watch_group.command("start")
@click.option("--daemon", is_flag=True)
def watch_start(daemon: bool) -> None:
    """Start fswatch for all monitored directories."""
    raise NotImplementedError("Phase 2: implement watcher.start")


@watch_group.command("stop")
def watch_stop() -> None:
    """Stop all fswatch processes."""
    raise NotImplementedError("Phase 2: implement watcher.stop")


@watch_group.command("logs")
@click.option("--dir", "dir_name", default=None)
@click.option("--tail", default=50, type=int)
def watch_logs(dir_name: str | None, tail: int) -> None:
    """Show recent fswatch events."""
    raise NotImplementedError("Phase 2: implement log tail")


@cli.command("diff")
@click.option("--dir", "dir_name", default=None)
def diff_cmd(dir_name: str | None) -> None:
    """Show what would sync (dry run)."""
    raise NotImplementedError("Phase 3: implement dry-run via syncer")


@cli.command("sync")
@click.option("--dir", "dir_name", default=None)
@click.option("--all", "sync_all", is_flag=True)
@click.option("--full", "full_sync", is_flag=True)
def sync_cmd(dir_name: str | None, sync_all: bool, full_sync: bool) -> None:
    """Sync changed files now."""
    raise NotImplementedError("Phase 3: implement hasher → syncer pipeline")


@cli.command("check")
@click.option("--checksum", is_flag=True)
@click.option("--dir", "dir_name", default=None)
def check_cmd(checksum: bool, dir_name: str | None) -> None:
    """Verify source matches destination."""
    raise NotImplementedError("Phase 3: implement rclone check wrapper")


@cli.command("changelog")
@click.option("--week", default=None)
def changelog_cmd(week: str | None) -> None:
    """Show this week's changes."""
    raise NotImplementedError("Phase 4: implement changelog.py")


@cli.group("schedule")
def schedule_group() -> None:
    """Install or remove cron jobs from config."""


@schedule_group.command("install")
def schedule_install() -> None:
    """Install cron jobs from config."""
    raise NotImplementedError("Phase 4: implement scheduler.install")


@schedule_group.command("remove")
def schedule_remove() -> None:
    """Remove all cron jobs installed by cloudsync."""
    raise NotImplementedError("Phase 4: implement scheduler.remove")


def main() -> None:
    cli()
