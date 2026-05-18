"""
cloudsync.config — YAML config loader and validator.

Phase 1 target: `cloudsync validate --config configs/test.yaml`

**Remote modes**

- **Mode A:** YAML has only ``remote.name`` and ``remote.bucket`` (no ``type``). The remote
  must already exist in ``rclone`` config; secrets never appear in YAML.
- **Mode B:** YAML includes ``remote.type`` plus ``provider`` and ``region``. Intended for
  future ``cloudsync setup`` / ``rclone config create`` flows (env vars or prompts).

This module:
  1. Loads a YAML file from disk
  2. Validates required fields, types, and paths
  3. Returns typed dataclasses used by every other module

Other modules import from here:
  from cloudsync.config import load_config, CloudSyncConfig
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


# ── Dataclasses ──────────────────────────────────────────────
# Typed config objects — every module reads from these, never raw dicts.

@dataclass
class ProjectConfig:
    name: str
    log_dir: str


@dataclass
class RemoteConfig:
    """
    Remote target for rclone.

    **Mode A (existing remote):** only ``name`` and ``bucket`` are set in YAML.
    Omit ``type`` (or use null). Credentials live in rclone's config only;
    ``cloudsync`` never reads keys from YAML. ``existing`` is True.

    **Mode B (new remote):** set ``type``, ``provider``, and ``region`` in YAML.
    Future ``cloudsync setup`` may run ``rclone config create`` using env vars or
    prompts. ``existing`` is False.
    """

    name: str
    bucket: str
    type: Optional[str] = None
    provider: Optional[str] = None
    region: Optional[str] = None
    existing: bool = False


@dataclass
class DirectoryConfig:
    name: str
    source: str
    dest: str
    watch: bool = True
    exclude: List[str] = field(default_factory=list)


@dataclass
class SyncConfig:
    transfers: int = 32
    checkers: int = 32
    checksum: bool = True
    backup_versions: bool = True
    version_dir: str = "versions"
    debounce_seconds: int = 30


@dataclass
class WeeklySchedule:
    enabled: bool = True
    day: str = "sunday"
    time: str = "02:00"
    max_age: str = "8d"


@dataclass
class MonthlySchedule:
    enabled: bool = True
    day: int = 1
    time: str = "03:00"


@dataclass
class ScheduleConfig:
    realtime: bool = True
    weekly_full: WeeklySchedule = field(default_factory=WeeklySchedule)
    monthly_audit: MonthlySchedule = field(default_factory=MonthlySchedule)


@dataclass
class CloudSyncConfig:
    """Root config object — passed to every module."""
    project: ProjectConfig
    remote: RemoteConfig
    directories: List[DirectoryConfig]
    sync: SyncConfig = field(default_factory=SyncConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    config_path: str = ""  # Path to the YAML file itself


# ── Validation Errors ────────────────────────────────────────

class ConfigError(Exception):
    """Raised when config validation fails."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Config has {len(errors)} error(s):\n" + "\n".join(f"  - {e}" for e in errors))


# ── Validators ───────────────────────────────────────────────

def _validate_raw(raw: dict) -> List[str]:
    """Validate raw YAML dict. Returns list of error strings (empty = valid)."""
    errors = []

    # --- Required top-level keys ---
    for key in ("project", "remote", "directories"):
        if key not in raw:
            errors.append(f"Missing required top-level key: '{key}'")

    if errors:
        return errors  # Can't proceed without required keys

    # --- Project section ---
    project = raw.get("project", {})
    if not isinstance(project, dict):
        errors.append("'project' must be a mapping")
    else:
        if not project.get("name"):
            errors.append("'project.name' is required")
        if not project.get("log_dir"):
            errors.append("'project.log_dir' is required")

    # --- Remote section ---
    remote = raw.get("remote", {})
    if not isinstance(remote, dict):
        errors.append("'remote' must be a mapping")
    else:
        for field_name in ("name", "bucket"):
            if not remote.get(field_name):
                errors.append(f"'remote.{field_name}' is required")

        raw_type = remote.get("type")
        if raw_type is not None and not isinstance(raw_type, str):
            errors.append("'remote.type' must be a string when specified")
        else:
            rtype = (raw_type or "").strip() if isinstance(raw_type, str) else None
            if rtype:
                if rtype not in ("s3", "gcs", "azure", "sftp"):
                    errors.append(
                        f"'remote.type' must be one of: s3, gcs, azure, sftp (got '{rtype}')"
                    )
                for field_name in ("provider", "region"):
                    if not remote.get(field_name):
                        errors.append(
                            f"'remote.{field_name}' is required when 'remote.type' is specified"
                        )

    # --- Directories section ---
    directories = raw.get("directories", [])
    if not isinstance(directories, list):
        errors.append("'directories' must be a list")
    elif len(directories) == 0:
        errors.append("'directories' must have at least one entry")
    else:
        seen_names = set()
        for i, d in enumerate(directories):
            prefix = f"directories[{i}]"
            if not isinstance(d, dict):
                errors.append(f"'{prefix}' must be a mapping")
                continue
            if not d.get("name"):
                errors.append(f"'{prefix}.name' is required")
            elif d["name"] in seen_names:
                errors.append(f"'{prefix}.name' duplicate name: '{d['name']}'")
            else:
                seen_names.add(d["name"])
            if not d.get("source"):
                errors.append(f"'{prefix}.source' is required")
            if not d.get("dest"):
                errors.append(f"'{prefix}.dest' is required")
            if d.get("exclude") and not isinstance(d["exclude"], list):
                errors.append(f"'{prefix}.exclude' must be a list")

    # --- Sync section (optional, validate if present) ---
    sync = raw.get("sync", {})
    if sync and isinstance(sync, dict):
        if "transfers" in sync and (not isinstance(sync["transfers"], int) or sync["transfers"] < 1):
            errors.append("'sync.transfers' must be a positive integer")
        if "checkers" in sync and (not isinstance(sync["checkers"], int) or sync["checkers"] < 1):
            errors.append("'sync.checkers' must be a positive integer")
        if "debounce_seconds" in sync and (not isinstance(sync["debounce_seconds"], int) or sync["debounce_seconds"] < 0):
            errors.append("'sync.debounce_seconds' must be a non-negative integer")

    # --- Schedule section (optional, validate if present) ---
    schedule = raw.get("schedule", {})
    if schedule and isinstance(schedule, dict):
        weekly = schedule.get("weekly_full", {})
        if weekly and isinstance(weekly, dict):
            valid_days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            if weekly.get("day") and weekly["day"].lower() not in valid_days:
                errors.append(f"'schedule.weekly_full.day' must be a day name (got '{weekly['day']}')")

    return errors


def _validate_paths(config: CloudSyncConfig, check_source_exists: bool = True) -> List[str]:
    """Validate that paths exist on disk. Separate from schema validation."""
    errors = []

    if check_source_exists:
        for d in config.directories:
            if not Path(d.source).exists():
                errors.append(f"Source path does not exist: '{d.source}' (directory '{d.name}')")

    return errors


# ── Parser ───────────────────────────────────────────────────

def _parse_config(raw: dict, config_path: str = "") -> CloudSyncConfig:
    """Convert validated raw dict into typed CloudSyncConfig."""
    project_raw = raw["project"]
    remote_raw = raw["remote"]
    dirs_raw = raw.get("directories", [])
    sync_raw = raw.get("sync", {}) or {}
    sched_raw = raw.get("schedule", {}) or {}

    # Expand ~ and env vars in paths
    log_dir = os.path.expandvars(os.path.expanduser(project_raw["log_dir"]))

    project = ProjectConfig(
        name=project_raw["name"],
        log_dir=log_dir,
    )

    raw_type = remote_raw.get("type")
    if isinstance(raw_type, str):
        raw_type = raw_type.strip() or None
    else:
        raw_type = None

    existing = raw_type is None

    remote = RemoteConfig(
        name=remote_raw["name"],
        bucket=remote_raw["bucket"],
        type=raw_type,
        provider=None if existing else remote_raw.get("provider"),
        region=None if existing else remote_raw.get("region"),
        existing=existing,
    )

    directories = []
    for d in dirs_raw:
        source = os.path.expandvars(os.path.expanduser(d["source"]))
        directories.append(DirectoryConfig(
            name=d["name"],
            source=source,
            dest=d["dest"],
            watch=d.get("watch", True),
            exclude=d.get("exclude", []),
        ))

    # Parse sync config with defaults
    sync = SyncConfig(
        transfers=sync_raw.get("transfers", 32),
        checkers=sync_raw.get("checkers", 32),
        checksum=sync_raw.get("checksum", True),
        backup_versions=sync_raw.get("backup_versions", True),
        version_dir=sync_raw.get("version_dir", "versions"),
        debounce_seconds=sync_raw.get("debounce_seconds", 30),
    )

    # Parse schedule config with defaults
    weekly_raw = sched_raw.get("weekly_full", {}) or {}
    monthly_raw = sched_raw.get("monthly_audit", {}) or {}

    schedule = ScheduleConfig(
        realtime=sched_raw.get("realtime", True),
        weekly_full=WeeklySchedule(
            enabled=weekly_raw.get("enabled", True),
            day=weekly_raw.get("day", "sunday"),
            time=weekly_raw.get("time", "02:00"),
            max_age=weekly_raw.get("max_age", "8d"),
        ),
        monthly_audit=MonthlySchedule(
            enabled=monthly_raw.get("enabled", True),
            day=monthly_raw.get("day", 1),
            time=monthly_raw.get("time", "03:00"),
        ),
    )

    return CloudSyncConfig(
        project=project,
        remote=remote,
        directories=directories,
        sync=sync,
        schedule=schedule,
        config_path=config_path,
    )


# ── Public API ───────────────────────────────────────────────

def load_config(path: str, check_paths: bool = True) -> CloudSyncConfig:
    """
    Load and validate a cloudsync YAML config file.

    Args:
        path: Path to the YAML config file
        check_paths: If True, verify source directories exist on disk

    Returns:
        CloudSyncConfig — typed config ready for use by other modules

    Raises:
        FileNotFoundError: If config file doesn't exist
        ConfigError: If validation fails (contains list of all errors)
    """
    config_path = Path(path).resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(["Config file must be a YAML mapping, got: " + type(raw).__name__])

    # Phase 1: Schema validation
    errors = _validate_raw(raw)
    if errors:
        raise ConfigError(errors)

    # Phase 2: Parse into typed objects
    config = _parse_config(raw, config_path=str(config_path))

    # Phase 3: Path validation (optional)
    if check_paths:
        path_errors = _validate_paths(config)
        if path_errors:
            raise ConfigError(path_errors)

    return config


def generate_template() -> str:
    """Return a starter YAML config template as a string."""
    return """# cloudsync.yaml — Config-driven file sync
# Generated by: cloudsync init
#
# Remote — pick ONE style:
#   Mode A (existing rclone remote): only `name` + `bucket`; omit `type`, `provider`, `region`.
#   Mode B (new remote, future setup): set `type`, `provider`, `region`; keys via env or CLI, never YAML.

project:
  name: my-sync-project
  log_dir: ~/cloudsync-logs

remote:
  name: mys3
  type: s3
  provider: AWS
  region: ap-south-1
  bucket: my-bucket-name

directories:
  - name: data
    source: /path/to/local/data
    dest: data
    watch: true
    exclude:
      - "*.tmp"
      - "__pycache__/**"

sync:
  transfers: 32
  checkers: 32
  checksum: true
  backup_versions: true
  version_dir: versions
  debounce_seconds: 30

schedule:
  realtime: true
  weekly_full:
    enabled: true
    day: sunday
    time: "02:00"
    max_age: 8d
  monthly_audit:
    enabled: true
    day: 1
    time: "03:00"
"""