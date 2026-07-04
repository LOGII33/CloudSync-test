"""
cloudsync.config — YAML config loader and validator.

Schedule structure:
  schedule:
    realtime:           sync every N minutes (expensive, optional)
    sync_schedule:      periodic sync — daily, weekly, custom cron
    integrity_audit:    monthly full comparison of all files
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import yaml


# ── Dataclasses ──────────────────────────────────────────────

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
class RealtimeSchedule:
    """Sync every N minutes. Uses smart_sync with scan_days=1."""
    enabled: bool = False
    interval_minutes: int = 30   # 30, 60, 120, 360, 720, 1440
    method: str = "smart"        # always smart — full would be insane here

@dataclass
class SyncSchedule:
    """Periodic sync — daily, weekly, or custom cron expression."""
    enabled: bool = True
    frequency: str = "weekly"       # daily, weekly, custom
    day: str = "sunday"             # for weekly: monday-sunday
    time: str = "02:00"             # HH:MM
    cron: str = ""                  # for custom: full cron expression
    method: str = "smart"           # smart or full
    scan_days: int = 8              # how many days of fswatch logs to read
    include_dir_scan: bool = True   # also scan directory mtime
    generate_report: bool = True    # create changelog

@dataclass
class IntegrityAudit:
    """Monthly full comparison of ALL files."""
    enabled: bool = True
    frequency: str = "monthly"      # monthly or weekly
    day: int = 1                    # day of month (1-28)
    time: str = "03:00"
    use_checksum: bool = False      # true=MD5 (slow), false=size only

@dataclass
class ScheduleConfig:
    realtime: RealtimeSchedule = field(default_factory=RealtimeSchedule)
    sync_schedule: SyncSchedule = field(default_factory=SyncSchedule)
    integrity_audit: IntegrityAudit = field(default_factory=IntegrityAudit)

@dataclass
class CloudSyncConfig:
    project: ProjectConfig
    remote: RemoteConfig
    directories: List[DirectoryConfig]
    sync: SyncConfig = field(default_factory=SyncConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    config_path: str = ""


# ── Errors ───────────────────────────────────────────────────

class ConfigError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"Config has {len(errors)} error(s):\n" + "\n".join(f"  - {e}" for e in errors))


# ── Validation ───────────────────────────────────────────────

VALID_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
VALID_FREQUENCIES = ("daily", "weekly", "custom")
VALID_METHODS = ("smart", "full")
VALID_INTERVALS = (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)

def _validate_raw(raw):
    errors = []

    # Required top-level keys
    for key in ("project", "remote", "directories"):
        if key not in raw: errors.append(f"Missing required key: '{key}'")
    if errors: return errors

    # Project
    p = raw.get("project", {})
    if not isinstance(p, dict): errors.append("'project' must be a mapping")
    else:
        if not p.get("name"): errors.append("'project.name' is required")
        if not p.get("log_dir"): errors.append("'project.log_dir' is required")

    # Remote
    r = raw.get("remote", {})
    if not isinstance(r, dict): errors.append("'remote' must be a mapping")
    else:
        for f in ("name", "bucket"):
            if not r.get(f): errors.append(f"'remote.{f}' is required")
        if r.get("type"):
            for f in ("provider", "region"):
                if not r.get(f): errors.append(f"'remote.{f}' is required when 'remote.type' is specified")
            if r["type"] not in ("s3", "gcs", "azure", "sftp"):
                errors.append("'remote.type' must be one of: s3, gcs, azure, sftp")

    # Directories
    dirs = raw.get("directories", [])
    if not isinstance(dirs, list): errors.append("'directories' must be a list")
    elif len(dirs) == 0: errors.append("'directories' must have at least one entry")
    else:
        seen = set()
        for i, d in enumerate(dirs):
            pf = f"directories[{i}]"
            if not isinstance(d, dict): errors.append(f"'{pf}' must be a mapping"); continue
            if not d.get("name"): errors.append(f"'{pf}.name' is required")
            elif d["name"] in seen: errors.append(f"'{pf}.name' duplicate: '{d['name']}'")
            else: seen.add(d["name"])
            if not d.get("source"): errors.append(f"'{pf}.source' is required")
            if not d.get("dest"): errors.append(f"'{pf}.dest' is required")

    # Sync
    sync = raw.get("sync", {})
    if sync and isinstance(sync, dict):
        if "transfers" in sync and (not isinstance(sync["transfers"], int) or sync["transfers"] < 1):
            errors.append("'sync.transfers' must be a positive integer")
        if "checkers" in sync and (not isinstance(sync["checkers"], int) or sync["checkers"] < 1):
            errors.append("'sync.checkers' must be a positive integer")

    # Schedule
    sched = raw.get("schedule", {})
    if sched and isinstance(sched, dict):
        # Realtime
        rt = sched.get("realtime", {})
        if rt and isinstance(rt, dict):
            interval = rt.get("interval_minutes")
            if interval is not None and (not isinstance(interval, int) or interval < 5):
                errors.append("'schedule.realtime.interval_minutes' must be >= 5")

        # Sync schedule
        ss = sched.get("sync_schedule", {})
        if ss and isinstance(ss, dict):
            freq = ss.get("frequency", "weekly")
            if freq not in VALID_FREQUENCIES:
                errors.append(f"'schedule.sync_schedule.frequency' must be one of: {', '.join(VALID_FREQUENCIES)}")
            if freq == "weekly" and ss.get("day") and ss["day"].lower() not in VALID_DAYS:
                errors.append(f"'schedule.sync_schedule.day' must be a day name")
            method = ss.get("method", "smart")
            if method not in VALID_METHODS:
                errors.append(f"'schedule.sync_schedule.method' must be: smart or full")
            scan_days = ss.get("scan_days")
            if scan_days is not None and (not isinstance(scan_days, int) or scan_days < 1):
                errors.append("'schedule.sync_schedule.scan_days' must be >= 1")

        # Integrity audit
        ia = sched.get("integrity_audit", {})
        if ia and isinstance(ia, dict):
            day = ia.get("day")
            if day is not None and (not isinstance(day, int) or day < 1 or day > 28):
                errors.append("'schedule.integrity_audit.day' must be 1-28")

    return errors


# ── Parser ───────────────────────────────────────────────────

def _parse_config(raw, config_path=""):
    pr = raw["project"]
    rr = raw["remote"]
    dr = raw.get("directories", [])
    sr = raw.get("sync", {}) or {}
    sched = raw.get("schedule", {}) or {}
    rt_raw = sched.get("realtime", {}) or {}
    ss_raw = sched.get("sync_schedule", {}) or {}
    ia_raw = sched.get("integrity_audit", {}) or {}

    log_dir = os.path.expandvars(os.path.expanduser(pr["log_dir"]))

    directories = []
    for d in dr:
        src = os.path.expandvars(os.path.expanduser(d["source"]))
        directories.append(DirectoryConfig(
            name=d["name"], source=src, dest=d["dest"],
            watch=d.get("watch", True), exclude=d.get("exclude", [])))

    # Handle old config format (weekly_full → sync_schedule)
    if not ss_raw and sched.get("weekly_full"):
        ss_raw = sched["weekly_full"]
    if not ia_raw and sched.get("monthly_audit"):
        ia_raw = sched["monthly_audit"]

    # Handle old format where realtime was just a bool
    if isinstance(rt_raw, bool):
        rt_raw = {"enabled": rt_raw}

    return CloudSyncConfig(
        project=ProjectConfig(name=pr["name"], log_dir=log_dir),
        remote=RemoteConfig(
            name=rr["name"], bucket=rr["bucket"], type=rr.get("type"),
            provider=rr.get("provider"), region=rr.get("region"),
            existing=rr.get("type") is None),
        directories=directories,
        sync=SyncConfig(
            transfers=sr.get("transfers", 32), checkers=sr.get("checkers", 32),
            checksum=sr.get("checksum", True), backup_versions=sr.get("backup_versions", True),
            version_dir=sr.get("version_dir", "versions"),
            debounce_seconds=sr.get("debounce_seconds", 30)),
        schedule=ScheduleConfig(
            realtime=RealtimeSchedule(
                enabled=rt_raw.get("enabled", False),
                interval_minutes=rt_raw.get("interval_minutes", 30),
                method=rt_raw.get("method", "smart")),
            sync_schedule=SyncSchedule(
                enabled=ss_raw.get("enabled", True),
                frequency=ss_raw.get("frequency", "weekly"),
                day=ss_raw.get("day", "sunday"),
                time=ss_raw.get("time", "02:00"),
                cron=ss_raw.get("cron", ""),
                method=ss_raw.get("method", "smart"),
                scan_days=ss_raw.get("scan_days", 8),
                include_dir_scan=ss_raw.get("include_dir_scan", True),
                generate_report=ss_raw.get("generate_report", True)),
            integrity_audit=IntegrityAudit(
                enabled=ia_raw.get("enabled", True),
                frequency=ia_raw.get("frequency", "monthly"),
                day=ia_raw.get("day", 1),
                time=ia_raw.get("time", "03:00"),
                use_checksum=ia_raw.get("use_checksum", False))),
        config_path=config_path)


# ── Public API ───────────────────────────────────────────────

def load_config(path, check_paths=True):
    cp = Path(path).resolve()
    if not cp.exists(): raise FileNotFoundError(f"Config file not found: {cp}")
    with open(cp) as f: raw = yaml.safe_load(f)
    if not isinstance(raw, dict): raise ConfigError(["Config file must be a YAML mapping"])
    errors = _validate_raw(raw)
    if errors: raise ConfigError(errors)
    config = _parse_config(raw, str(cp))
    if check_paths:
        for d in config.directories:
            if not Path(d.source).exists():
                raise ConfigError([f"Source not found: '{d.source}' (directory '{d.name}')"])
    return config


def generate_template():
    return """# cloudsync.yaml — Config-driven file sync
# Run: cloudsync validate --config cloudsync.yaml

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

schedule:
  # ── Realtime sync (every N minutes) ──
  # Uses smart_sync with today's fswatch logs
  # WARNING: costs more API calls. Disable for large datasets.
  realtime:
    enabled: false          # true = cron runs every interval_minutes
    interval_minutes: 60    # sync every 60 minutes (min: 5)
    # method is always smart (full would be too expensive)

  # ── Scheduled sync (daily/weekly) ──
  # Primary sync — uses smart_sync with fswatch + dir scan
  sync_schedule:
    enabled: true
    frequency: weekly       # daily, weekly, or custom
    day: sunday             # for weekly only
    time: "02:00"           # HH:MM
    # cron: "0 2 * * 0,3"  # for custom: raw cron expression
    method: smart           # smart = fswatch+scan+checksum, full = rclone direct
    scan_days: 8            # days of fswatch logs to read
    include_dir_scan: true  # also scan directory mtime as backup
    generate_report: true   # create weekly changelog

  # ── Integrity audit ──
  # Full comparison of ALL files — safety net
  integrity_audit:
    enabled: true
    frequency: monthly      # monthly or weekly
    day: 1                  # day of month (1-28)
    time: "03:00"
    use_checksum: false     # true=MD5 hash (slow), false=size only (fast)
"""
