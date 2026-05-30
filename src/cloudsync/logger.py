"""
cloudsync.logger — Structured logging for all components.

Creates per-component log files:
  {log_dir}/cloudsync.log        — main application log
  {log_dir}/fswatch/{dir_name}.log — per-directory fswatch events
  {log_dir}/sync/sync-YYYY-MM-DD.log — sync operation logs
  {log_dir}/changelogs/          — weekly change reports

Usage:
  from cloudsync.logger import setup_logging, get_logger
  setup_logging(config)
  log = get_logger("watcher")
  log.info("Started watching", extra={"directory": "AIML"})
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from cloudsync.config import CloudSyncConfig


# ── Custom Formatter ─────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """JSON-structured log formatter for machine-readable logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }

        # Add any extra fields passed via extra={}
        for key in ("directory", "file_path", "event_type", "pid",
                     "file_count", "duration_seconds", "error"):
            value = getattr(record, key, None)
            if value is not None:
                log_entry[key] = value

        return json.dumps(log_entry)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter for console output."""

    COLORS = {
        "DEBUG": "\033[90m",     # gray
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[1;31m", # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%H:%M:%S")
        component = record.name.replace("cloudsync.", "")

        # Add directory context if available
        directory = getattr(record, "directory", "")
        dir_str = f"[{directory}] " if directory else ""

        return f"{color}{timestamp} {record.levelname:<7}{self.RESET} {component:>10} | {dir_str}{record.getMessage()}"


# ── Setup Functions ──────────────────────────────────────────

def ensure_log_dirs(config: CloudSyncConfig) -> dict:
    """Create all log directories and return their paths."""
    base = Path(config.project.log_dir)
    dirs = {
        "base": base,
        "fswatch": base / "fswatch",
        "sync": base / "sync",
        "changelogs": base / "changelogs",
        "pids": base / "pids",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in dirs.items()}


def setup_logging(config: CloudSyncConfig, console: bool = True, level: str = "INFO") -> dict:
    """
    Set up logging for all cloudsync components.

    Args:
        config: CloudSyncConfig with log_dir path
        console: If True, also log to console with color
        level: Log level string (DEBUG, INFO, WARNING, ERROR)

    Returns:
        dict of log directory paths
    """
    log_dirs = ensure_log_dirs(config)

    # Root cloudsync logger
    root_logger = logging.getLogger("cloudsync")
    root_logger.setLevel(getattr(logging, level.upper()))
    root_logger.handlers.clear()

    # File handler — structured JSON logs
    log_file = Path(log_dirs["base"]) / "cloudsync.log"
    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setFormatter(StructuredFormatter())
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    # Console handler — human-readable
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(HumanFormatter())
        console_handler.setLevel(getattr(logging, level.upper()))
        root_logger.addHandler(console_handler)

    return log_dirs


def get_logger(component: str) -> logging.Logger:
    """Get a logger for a specific component (e.g., 'watcher', 'syncer')."""
    return logging.getLogger(f"cloudsync.{component}")
