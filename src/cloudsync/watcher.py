"""
cloudsync.watcher — fswatch subprocess manager.

Phase 2 target:
  cloudsync watch start --config tamizh.yaml
  cloudsync watch stop --config tamizh.yaml
  cloudsync watch status --config tamizh.yaml
  cloudsync watch logs --dir AIML --tail 50

This module:
  1. Detects the OS and selects the best fswatch monitor driver
  2. Starts one fswatch subprocess per watched directory
  3. Parses fswatch output into structured per-directory logs
  4. Manages PID files for start/stop lifecycle
  5. Tracks metadata (start time, event counts, last event, monitor used)
  6. Provides log reading/filtering for the CLI

Architecture:
  cloudsync watch start
    → detect_monitor() — OS-aware driver selection (run once)
    → reads config.directories (watch=True only)
    → for each directory:
        → spawns fswatch subprocess in background (with --monitor <driver>)
        → stdout piped to a log parser thread
        → PID saved to {log_dir}/pids/{dir_name}.pid
        → metadata saved to {log_dir}/pids/{dir_name}.meta.json
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cloudsync.config import CloudSyncConfig, DirectoryConfig
from cloudsync.logger import get_logger, ensure_log_dirs

log = get_logger("watcher")


# ── Monitor Priority Maps ─────────────────────────────────────

# Per-OS ordered preference: first available monitor wins.
MONITOR_PRIORITY: Dict[str, List[str]] = {
    "Linux":   ["inotify_monitor", "poll_monitor"],
    "Darwin":  ["fsevents_monitor", "kqueue_monitor", "poll_monitor"],
    "FreeBSD": ["kqueue_monitor", "poll_monitor"],
    "OpenBSD": ["kqueue_monitor", "poll_monitor"],
    "NetBSD":  ["kqueue_monitor", "poll_monitor"],
}

# Linux inotify tunables — warn if below these values.
INOTIFY_RECOMMENDED = {
    "max_user_watches":   524288,
    "max_queued_events":  32768,
}


# ── SystemInfo Dataclass ─────────────────────────────────────

@dataclass
class SystemInfo:
    """Captured OS + fswatch environment at startup."""
    os_name: str
    os_release: str
    os_arch: str
    selected_monitor: str
    available_monitors: List[str] = field(default_factory=list)
    inotify_limits: Dict[str, int] = field(default_factory=dict)
    fallback_used: bool = False
    warnings: List[str] = field(default_factory=list)


# ── Data Structures ──────────────────────────────────────────

class WatcherMetadata:
    """Tracks runtime metadata for a single directory watcher."""

    def __init__(self, dir_name: str, source_path: str):
        self.dir_name = dir_name
        self.source_path = source_path
        self.pid: Optional[int] = None
        self.started_at: Optional[str] = None
        self.stopped_at: Optional[str] = None
        self.event_count: int = 0
        self.last_event_at: Optional[str] = None
        self.last_event_path: Optional[str] = None
        self.status: str = "stopped"  # stopped, running, error
        self.monitor: Optional[str] = None  # fswatch monitor driver used

    def to_dict(self) -> dict:
        return {
            "dir_name": self.dir_name,
            "source_path": self.source_path,
            "pid": self.pid,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "event_count": self.event_count,
            "last_event_at": self.last_event_at,
            "last_event_path": self.last_event_path,
            "status": self.status,
            "monitor": self.monitor,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WatcherMetadata":
        meta = cls(data["dir_name"], data["source_path"])
        meta.pid = data.get("pid")
        meta.started_at = data.get("started_at")
        meta.stopped_at = data.get("stopped_at")
        meta.event_count = data.get("event_count", 0)
        meta.last_event_at = data.get("last_event_at")
        meta.last_event_path = data.get("last_event_path")
        meta.status = data.get("status", "stopped")
        meta.monitor = data.get("monitor")
        return meta


class FswatchEvent:
    """Parsed fswatch event."""

    def __init__(self, timestamp: str, path: str, event_type: str, dir_name: str):
        self.timestamp = timestamp
        self.path = path
        self.event_type = event_type
        self.dir_name = dir_name

    def to_log_line(self) -> str:
        return f"{self.timestamp} {self.event_type:<10} {self.path}"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "path": self.path,
            "event_type": self.event_type,
            "dir_name": self.dir_name,
        }


# ── OS Detection & Monitor Selection ─────────────────────────

def _get_available_monitors() -> List[str]:
    """
    Query fswatch --list-monitors for available monitor names.
    Returns an empty list if fswatch is not installed or errors.
    """
    try:
        result = subprocess.run(
            ["fswatch", "--list-monitors"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _read_inotify_limit(key: str) -> Optional[int]:
    """Read a single value from /proc/sys/fs/inotify/ (Linux only)."""
    path = Path(f"/proc/sys/fs/inotify/{key}")
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def detect_monitor() -> SystemInfo:
    """
    Detect the current OS and choose the best fswatch monitor driver.

    Priority order per OS is defined in MONITOR_PRIORITY.  Falls back to
    poll_monitor with a warning when nothing better is available, and also
    warns on Linux when inotify kernel limits are below recommended thresholds.

    Returns a SystemInfo capturing everything discovered.
    """
    os_name = platform.system()
    os_release = platform.release()
    os_arch = platform.machine()

    available = _get_available_monitors()

    info = SystemInfo(
        os_name=os_name,
        os_release=os_release,
        os_arch=os_arch,
        selected_monitor="poll_monitor",
        available_monitors=available,
    )

    if not available:
        info.warnings.append(
            "fswatch not found or returned no monitors — "
            "install fswatch and ensure it is on PATH."
        )
        info.fallback_used = True
        log.warning(info.warnings[-1])
        return info

    priority = MONITOR_PRIORITY.get(os_name)
    unknown_os = priority is None
    if unknown_os:
        priority = ["poll_monitor"]

    selected = None
    for candidate in priority:
        if candidate in available:
            selected = candidate
            break

    if selected is None:
        # None of the preferred monitors available; fall back to poll
        selected = "poll_monitor"
        info.fallback_used = True
        warning = (
            f"No preferred monitor found for OS '{os_name}' "
            f"(available: {available}). Falling back to poll_monitor."
        )
        info.warnings.append(warning)
        log.warning(warning)
    elif selected == "poll_monitor" and (unknown_os or len(priority) > 1):
        # The only available monitor is poll (preferred list had better options)
        info.fallback_used = True
        warning = (
            f"Best available monitor for '{os_name}' is poll_monitor — "
            f"consider installing a native backend."
        )
        info.warnings.append(warning)
        log.warning(warning)

    info.selected_monitor = selected
    log.info(
        f"OS: {os_name} {os_release} ({os_arch}) — using fswatch monitor: {selected}",
        extra={"monitor": selected, "os": os_name},
    )

    # ── Linux: check inotify kernel limits ───────────────────
    if os_name == "Linux":
        for key, recommended in INOTIFY_RECOMMENDED.items():
            value = _read_inotify_limit(key)
            if value is not None:
                info.inotify_limits[key] = value
                if value < recommended:
                    warning = (
                        f"inotify {key} is {value} (recommended ≥ {recommended}). "
                        f"Increase with: "
                        f"sudo sysctl fs.inotify.{key}={recommended}"
                    )
                    info.warnings.append(warning)
                    log.warning(warning)

    return info


# ── Path Helpers ─────────────────────────────────────────────

def _pid_file(config: CloudSyncConfig, dir_name: str) -> Path:
    return Path(config.project.log_dir) / "pids" / f"{dir_name}.pid"


def _meta_file(config: CloudSyncConfig, dir_name: str) -> Path:
    return Path(config.project.log_dir) / "pids" / f"{dir_name}.meta.json"


def _event_log(config: CloudSyncConfig, dir_name: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return Path(config.project.log_dir) / "fswatch" / f"{dir_name}-{today}.log"


def _save_metadata(config: CloudSyncConfig, meta: WatcherMetadata):
    meta_path = _meta_file(config, meta.dir_name)
    meta_path.write_text(json.dumps(meta.to_dict(), indent=2))


def _load_metadata(config: CloudSyncConfig, dir_name: str) -> Optional[WatcherMetadata]:
    meta_path = _meta_file(config, dir_name)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text())
            return WatcherMetadata.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None
    return None


# ── Process Management ───────────────────────────────────────

def _is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)  # Signal 0 = just check existence
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _build_fswatch_cmd(
    dir_config: DirectoryConfig,
    monitor: Optional[str] = None,
) -> List[str]:
    """
    Build the fswatch command for a directory.

    When *monitor* is provided (and is not poll_monitor or an unknown name),
    ``--monitor <monitor>`` is prepended so fswatch uses the OS-native backend.
    poll_monitor is fswatch's own default fallback so we skip the flag for it.
    """
    cmd = ["fswatch"]

    # Inject --monitor only when we have a real native driver
    if monitor and monitor not in ("poll_monitor",):
        cmd.extend(["--monitor", monitor])

    cmd.extend([
        "--recursive",
        "--event-flags",              # Include event type in output
        "--timestamp",                # Prepend timestamp to each line
        "--event", "Created",
        "--event", "Updated",
        "--event", "Removed",
        "--event", "Renamed",
        "--event", "MovedFrom",
        "--event", "MovedTo",
    ])

    # Add exclude filters from config
    for pattern in dir_config.exclude:
        cmd.extend(["--exclude", pattern])

    # Add the source directory to watch
    cmd.append(dir_config.source)

    return cmd


def _parse_fswatch_line(line: str, dir_name: str, source_path: str) -> Optional[FswatchEvent]:
    """
    Parse a single line of fswatch output.

    fswatch --timestamp --event-flags output format:
      Thu Apr 24 14:32:01 2026 /path/to/file Created Updated
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split()
    if len(parts) < 2:
        return None

    timestamp = datetime.now().isoformat()

    # Common fswatch flags
    known_events = {"Created", "Updated", "Removed", "Renamed", "MovedFrom",
                    "MovedTo", "IsFile", "IsDir", "IsSymLink", "Link",
                    "AttributeModified", "OwnerModified"}

    # Find where the path ends and flags begin
    path_parts = []
    event_parts = []
    for part in parts:
        if part in known_events:
            event_parts.append(part)
        elif event_parts:
            event_parts.append(part)
        else:
            path_parts.append(part)

    if not path_parts:
        return None

    file_path = " ".join(path_parts)
    rel_path = file_path.replace(source_path, "").lstrip("/")

    primary_events = {"Created", "Updated", "Removed", "Renamed", "MovedFrom", "MovedTo"}
    event_type = "Modified"  # default
    for evt in event_parts:
        if evt in primary_events:
            event_type = evt
            break

    return FswatchEvent(
        timestamp=timestamp,
        path=rel_path or file_path,
        event_type=event_type,
        dir_name=dir_name,
    )


def _log_writer_thread(
    process: subprocess.Popen,
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    meta: WatcherMetadata,
):
    """
    Background thread that reads fswatch stdout and writes to log files.
    Runs until the fswatch process exits.
    """
    dir_name = dir_config.name
    watcher_log = get_logger(f"watcher.{dir_name}")

    try:
        for raw_line in iter(process.stdout.readline, ""):
            if not raw_line:
                break

            event = _parse_fswatch_line(raw_line, dir_name, dir_config.source)
            if not event:
                continue

            # Update metadata
            meta.event_count += 1
            meta.last_event_at = event.timestamp
            meta.last_event_path = event.path
            _save_metadata(config, meta)

            # Write to per-directory log file
            log_file = _event_log(config, dir_name)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(event.to_log_line() + "\n")

            watcher_log.debug(
                f"{event.event_type}: {event.path}",
                extra={"directory": dir_name, "event_type": event.event_type,
                       "file_path": event.path},
            )

    except Exception as e:
        watcher_log.error(f"Log writer error: {e}", extra={"directory": dir_name, "error": str(e)})
    finally:
        meta.status = "stopped"
        meta.stopped_at = datetime.now().isoformat()
        _save_metadata(config, meta)


# ── Public API ───────────────────────────────────────────────

def get_watched_directories(config: CloudSyncConfig) -> List[DirectoryConfig]:
    """Return only directories with watch=True."""
    return [d for d in config.directories if d.watch]


def start_watcher(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    system_info: Optional[SystemInfo] = None,
) -> WatcherMetadata:
    """
    Start fswatch for a single directory.

    Args:
        config:      CloudSyncConfig
        dir_config:  The directory to watch
        system_info: Optional pre-computed SystemInfo from detect_monitor().
                     When None, detect_monitor() is called automatically.

    Returns WatcherMetadata with PID, status, and monitor used.
    Raises RuntimeError if already running or source doesn't exist.
    """
    dir_name = dir_config.name

    # Check if already running
    existing_meta = _load_metadata(config, dir_name)
    if existing_meta and existing_meta.pid and _is_process_alive(existing_meta.pid):
        raise RuntimeError(
            f"Watcher for '{dir_name}' is already running (PID {existing_meta.pid})"
        )

    # Verify source directory exists
    if not Path(dir_config.source).exists():
        raise FileNotFoundError(f"Source directory not found: {dir_config.source}")

    # Ensure log directories exist
    ensure_log_dirs(config)

    # Detect monitor if not provided
    if system_info is None:
        system_info = detect_monitor()

    monitor = system_info.selected_monitor

    # Build and start fswatch command with the selected monitor
    cmd = _build_fswatch_cmd(dir_config, monitor=monitor)
    log.info(f"Starting fswatch for '{dir_name}'", extra={"directory": dir_name})
    log.debug(f"Command: {' '.join(cmd)}", extra={"directory": dir_name})

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
    )

    # Create metadata
    meta = WatcherMetadata(dir_name=dir_name, source_path=dir_config.source)
    meta.pid = process.pid
    meta.started_at = datetime.now().isoformat()
    meta.status = "running"
    meta.monitor = monitor

    # Save PID file
    pid_file = _pid_file(config, dir_name)
    pid_file.write_text(str(process.pid))

    # Save metadata
    _save_metadata(config, meta)

    # Start log writer thread
    thread = threading.Thread(
        target=_log_writer_thread,
        args=(process, config, dir_config, meta),
        daemon=True,
        name=f"fswatch-{dir_name}",
    )
    thread.start()

    log.info(
        f"Watcher started for '{dir_name}' (PID {process.pid}, monitor: {monitor})",
        extra={"directory": dir_name, "pid": process.pid, "monitor": monitor},
    )

    return meta


def stop_watcher(config: CloudSyncConfig, dir_name: str) -> bool:
    """
    Stop fswatch for a single directory.

    Returns True if stopped, False if wasn't running.
    """
    pid_file = _pid_file(config, dir_name)

    if not pid_file.exists():
        log.warning(f"No PID file for '{dir_name}'", extra={"directory": dir_name})
        return False

    pid = int(pid_file.read_text().strip())

    if not _is_process_alive(pid):
        log.info(f"Process {pid} for '{dir_name}' already dead", extra={"directory": dir_name})
        pid_file.unlink(missing_ok=True)
        meta = _load_metadata(config, dir_name)
        if meta:
            meta.status = "stopped"
            meta.stopped_at = datetime.now().isoformat()
            _save_metadata(config, meta)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log.info(f"Sent SIGTERM to PID {pid} for '{dir_name}'",
                 extra={"directory": dir_name, "pid": pid})

        for _ in range(50):
            if not _is_process_alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            log.warning(f"Force killed PID {pid} for '{dir_name}'",
                        extra={"directory": dir_name, "pid": pid})

    except ProcessLookupError:
        pass  # Already dead

    pid_file.unlink(missing_ok=True)

    meta = _load_metadata(config, dir_name)
    if meta:
        meta.status = "stopped"
        meta.stopped_at = datetime.now().isoformat()
        meta.pid = None
        _save_metadata(config, meta)

    log.info(f"Watcher stopped for '{dir_name}'", extra={"directory": dir_name})
    return True


def start_all(config: CloudSyncConfig) -> Dict[str, WatcherMetadata]:
    """
    Start watchers for all watched directories.

    detect_monitor() is called exactly once and the result is reused across
    all directories so OS detection and inotify-limit warnings appear only once.

    Returns dict of name→metadata.
    """
    system_info = detect_monitor()
    results = {}
    for dir_config in get_watched_directories(config):
        try:
            meta = start_watcher(config, dir_config, system_info=system_info)
            results[dir_config.name] = meta
        except (RuntimeError, FileNotFoundError) as e:
            log.error(f"Failed to start watcher for '{dir_config.name}': {e}",
                      extra={"directory": dir_config.name, "error": str(e)})
            meta = WatcherMetadata(dir_config.name, dir_config.source)
            meta.status = "error"
            results[dir_config.name] = meta
    return results


def stop_all(config: CloudSyncConfig) -> Dict[str, bool]:
    """Stop all watchers. Returns dict of name→stopped."""
    results = {}
    for dir_config in get_watched_directories(config):
        results[dir_config.name] = stop_watcher(config, dir_config.name)
    return results


def get_status(config: CloudSyncConfig) -> Dict[str, WatcherMetadata]:
    """Get current status of all watchers."""
    statuses = {}
    for dir_config in get_watched_directories(config):
        meta = _load_metadata(config, dir_config.name)
        if meta:
            if meta.pid and not _is_process_alive(meta.pid):
                meta.status = "dead"
                meta.stopped_at = meta.stopped_at or datetime.now().isoformat()
                _save_metadata(config, meta)
            statuses[dir_config.name] = meta
        else:
            statuses[dir_config.name] = WatcherMetadata(dir_config.name, dir_config.source)
    return statuses


# ── Log Reading ──────────────────────────────────────────────

def read_events(config: CloudSyncConfig, dir_name: str,
                tail: int = 50, date: Optional[str] = None) -> List[str]:
    """
    Read recent events from a directory's fswatch log.

    Args:
        config:   CloudSyncConfig
        dir_name: Directory name to read logs for
        tail:     Number of recent lines to return
        date:     Specific date (YYYY-MM-DD) or None for today

    Returns:
        List of log lines (most recent last)
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    log_file = Path(config.project.log_dir) / "fswatch" / f"{dir_name}-{date}.log"

    if not log_file.exists():
        return []

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    lines = [l for l in lines if l.strip()]

    if tail and tail < len(lines):
        return lines[-tail:]
    return lines


def get_changed_files(config: CloudSyncConfig, dir_name: str,
                      date: Optional[str] = None) -> List[str]:
    """
    Get deduplicated list of changed file paths from fswatch logs.
    This is what gets fed to rclone's --files-from in Phase 3.

    Returns:
        Sorted list of unique relative file paths that changed
    """
    events = read_events(config, dir_name, tail=None, date=date)
    paths = set()
    for line in events:
        parts = line.split(maxsplit=2)
        if len(parts) >= 3:
            paths.add(parts[2])
        elif len(parts) == 2:
            paths.add(parts[1])
    return sorted(paths)
