"""
cloudsync.config — YAML config loader and validator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class ProjectConfig:
    name: str
    log_dir: str

@dataclass
class RemoteConfig:
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
    project: ProjectConfig
    remote: RemoteConfig
    directories: List[DirectoryConfig]
    sync: SyncConfig = field(default_factory=SyncConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    config_path: str = ""


class ConfigError(Exception):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Config has {len(errors)} error(s):\n" + "\n".join(f"  - {e}" for e in errors))


def _validate_raw(raw: dict) -> List[str]:
    errors = []
    for key in ("project", "remote", "directories"):
        if key not in raw:
            errors.append(f"Missing required top-level key: '{key}'")
    if errors:
        return errors

    project = raw.get("project", {})
    if not isinstance(project, dict):
        errors.append("'project' must be a mapping")
    else:
        if not project.get("name"):
            errors.append("'project.name' is required")
        if not project.get("log_dir"):
            errors.append("'project.log_dir' is required")

    remote = raw.get("remote", {})
    if not isinstance(remote, dict):
        errors.append("'remote' must be a mapping")
    else:
        for f in ("name", "bucket"):
            if not remote.get(f):
                errors.append(f"'remote.{f}' is required")
        if remote.get("type"):
            for f in ("provider", "region"):
                if not remote.get(f):
                    errors.append(f"'remote.{f}' is required when 'remote.type' is specified")
            if remote["type"] not in ("s3", "gcs", "azure", "sftp"):
                errors.append(f"'remote.type' must be one of: s3, gcs, azure, sftp")

    directories = raw.get("directories", [])
    if not isinstance(directories, list):
        errors.append("'directories' must be a list")
    elif len(directories) == 0:
        errors.append("'directories' must have at least one entry")
    else:
        seen = set()
        for i, d in enumerate(directories):
            p = f"directories[{i}]"
            if not isinstance(d, dict):
                errors.append(f"'{p}' must be a mapping")
                continue
            if not d.get("name"):
                errors.append(f"'{p}.name' is required")
            elif d["name"] in seen:
                errors.append(f"'{p}.name' duplicate name: '{d['name']}'")
            else:
                seen.add(d["name"])
            if not d.get("source"):
                errors.append(f"'{p}.source' is required")
            if not d.get("dest"):
                errors.append(f"'{p}.dest' is required")
            if d.get("exclude") and not isinstance(d["exclude"], list):
                errors.append(f"'{p}.exclude' must be a list")

    sync = raw.get("sync", {})
    if sync and isinstance(sync, dict):
        if "transfers" in sync and (not isinstance(sync["transfers"], int) or sync["transfers"] < 1):
            errors.append("'sync.transfers' must be a positive integer")
        if "checkers" in sync and (not isinstance(sync["checkers"], int) or sync["checkers"] < 1):
            errors.append("'sync.checkers' must be a positive integer")
        if "debounce_seconds" in sync and (not isinstance(sync["debounce_seconds"], int) or sync["debounce_seconds"] < 0):
            errors.append("'sync.debounce_seconds' must be a non-negative integer")

    schedule = raw.get("schedule", {})
    if schedule and isinstance(schedule, dict):
        weekly = schedule.get("weekly_full", {})
        if weekly and isinstance(weekly, dict):
            valid_days = ("monday","tuesday","wednesday","thursday","friday","saturday","sunday")
            if weekly.get("day") and weekly["day"].lower() not in valid_days:
                errors.append(f"'schedule.weekly_full.day' must be a day name (got '{weekly['day']}')")

    return errors


def _validate_paths(config: CloudSyncConfig, check: bool = True) -> List[str]:
    errors = []
    if check:
        for d in config.directories:
            if not Path(d.source).exists():
                errors.append(f"Source path does not exist: '{d.source}' (directory '{d.name}')")
    return errors


def _parse_config(raw: dict, config_path: str = "") -> CloudSyncConfig:
    project_raw = raw["project"]
    remote_raw = raw["remote"]
    dirs_raw = raw.get("directories", [])
    sync_raw = raw.get("sync", {}) or {}
    sched_raw = raw.get("schedule", {}) or {}

    log_dir = os.path.expandvars(os.path.expanduser(project_raw["log_dir"]))

    project = ProjectConfig(name=project_raw["name"], log_dir=log_dir)
    remote = RemoteConfig(
        name=remote_raw["name"],
        bucket=remote_raw["bucket"],
        type=remote_raw.get("type"),
        provider=remote_raw.get("provider"),
        region=remote_raw.get("region"),
        existing=remote_raw.get("type") is None,
    )

    directories = []
    for d in dirs_raw:
        source = os.path.expandvars(os.path.expanduser(d["source"]))
        directories.append(DirectoryConfig(
            name=d["name"], source=source, dest=d["dest"],
            watch=d.get("watch", True), exclude=d.get("exclude", []),
        ))

    weekly_raw = sched_raw.get("weekly_full", {}) or {}
    monthly_raw = sched_raw.get("monthly_audit", {}) or {}

    sync = SyncConfig(
        transfers=sync_raw.get("transfers", 32), checkers=sync_raw.get("checkers", 32),
        checksum=sync_raw.get("checksum", True), backup_versions=sync_raw.get("backup_versions", True),
        version_dir=sync_raw.get("version_dir", "versions"), debounce_seconds=sync_raw.get("debounce_seconds", 30),
    )
    schedule = ScheduleConfig(
        realtime=sched_raw.get("realtime", True),
        weekly_full=WeeklySchedule(
            enabled=weekly_raw.get("enabled", True), day=weekly_raw.get("day", "sunday"),
            time=weekly_raw.get("time", "02:00"), max_age=weekly_raw.get("max_age", "8d"),
        ),
        monthly_audit=MonthlySchedule(
            enabled=monthly_raw.get("enabled", True), day=monthly_raw.get("day", 1),
            time=monthly_raw.get("time", "03:00"),
        ),
    )

    return CloudSyncConfig(project=project, remote=remote, directories=directories,
                            sync=sync, schedule=schedule, config_path=config_path)


def load_config(path: str, check_paths: bool = True) -> CloudSyncConfig:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError(["Config file must be a YAML mapping, got: " + type(raw).__name__])
    errors = _validate_raw(raw)
    if errors:
        raise ConfigError(errors)
    config = _parse_config(raw, config_path=str(config_path))
    if check_paths:
        path_errors = _validate_paths(config)
        if path_errors:
            raise ConfigError(path_errors)
    return config


def generate_template() -> str:
    return """# cloudsync.yaml — Config-driven file sync
# Generated by: cloudsync init

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
