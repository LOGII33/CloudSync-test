"""
cloudsync.syncer — rclone sync engine with merged smart sync pipeline.

smart_sync pipeline:
  fswatch logs → dir mtime scan → merge → size+mtime filter → checksum → sync

Two modes:
  smart_sync:  fswatch + scan + size/mtime + checksum (primary, cheap)
  full_sync:   rclone --max-age direct scan (last resort, expensive)
"""
from __future__ import annotations
import json, subprocess, tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from cloudsync.config import CloudSyncConfig, DirectoryConfig
from cloudsync.hasher import (compare_checksums, compare_size_mtime,
                               _build_remote_path, _write_files_from)
from cloudsync.watcher import (get_changed_files, get_changed_files_range,
                                get_watched_directories, find_recent_files)
from cloudsync.logger import get_logger, ensure_log_dirs
log = get_logger("syncer")


# ── Sync Result ──────────────────────────────────────────────

@dataclass
class SyncResult:
    dir_name: str
    status: str = "pending"
    files_from_fswatch: int = 0
    files_from_scan: int = 0
    files_merged: int = 0
    files_after_size_mtime: int = 0
    files_after_checksum: int = 0
    files_new: int = 0
    files_synced: int = 0
    files_failed: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0
    sync_mode: str = ""
    dry_run: bool = False
    fswatch_coverage: str = ""
    errors: List[str] = field(default_factory=list)
    synced_files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ── rclone Command Builders ──────────────────────────────────

def _build_copy_cmd(config, dir_config, files_from_path=None,
                     dry_run=False, max_age=None):
    remote = _build_remote_path(config, dir_config)
    cmd = ["rclone", "copy", dir_config.source, remote,
           "--transfers", str(config.sync.transfers),
           "--checkers", str(config.sync.checkers),
           "--retries", "10", "--retries-sleep", "5s", "--stats", "0"]
    if files_from_path: cmd.extend(["--files-from", files_from_path])
    if config.sync.checksum: cmd.append("--checksum")
    if dry_run: cmd.append("--dry-run")
    if max_age: cmd.extend(["--max-age", max_age])
    for p in dir_config.exclude: cmd.extend(["--exclude", p])
    if config.sync.backup_versions and not dry_run:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        cmd.extend(["--backup-dir",
                     f"{config.remote.name}:{config.remote.bucket}/{config.sync.version_dir}/{ts}"])
    return cmd


def _build_verify_cmd(config, dir_config, files_from_path):
    remote = _build_remote_path(config, dir_config)
    return ["rclone", "check", dir_config.source, remote,
            "--files-from", files_from_path, "--one-way", "--size-only"]


def _run_rclone(cmd, timeout=3600):
    log.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── Smart Sync (merged pipeline) ────────────────────────────

def smart_sync(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    dry_run: bool = False,
    date: Optional[str] = None,
    scan_days: int = 1,
    include_dir_scan: bool = False,
    generate_report: bool = False,
) -> SyncResult:
    """
    Smart sync: the full production pipeline.

    Pipeline:
      1. fswatch logs (scan_days days) → changed files        [FREE]
      2. Directory mtime scan (if include_dir_scan) → more    [FREE]
      3. Merge + deduplicate                                   [FREE]
      4. Size + mtime filter → eliminate unchanged             [CHEAP]
      5. Checksum verify → confirm real changes                [MODERATE]
      6. Sync only confirmed changes                           [CHEAP]
      7. Verify + report                                       [CHEAP]

    For on-demand:  scan_days=1, include_dir_scan=False
    For weekly:     scan_days=8, include_dir_scan=True, generate_report=True
    """
    result = SyncResult(
        dir_name=dir_config.name,
        sync_mode="smart",
        dry_run=dry_run,
        started_at=datetime.now().isoformat(),
    )
    start_time = datetime.now()

    try:
        # ── Stage 1: Collect from fswatch logs ──
        if scan_days > 1:
            fswatch_files = set(get_changed_files_range(config, dir_config.name, days=scan_days))
        else:
            fswatch_files = set(get_changed_files(config, dir_config.name, date=date))

        result.files_from_fswatch = len(fswatch_files)
        log.info(f"Stage 1: {len(fswatch_files)} files from fswatch ({scan_days} day(s))",
                 extra={"directory": dir_config.name, "file_count": len(fswatch_files)})

        # ── Stage 2: Directory mtime scan (optional) ──
        scan_files = set()
        if include_dir_scan:
            scan_max_age_str = f"{config.schedule.sync_schedule.scan_days}d"
            scan_days_num = int(scan_max_age_str.rstrip("d"))
            scan_files = set(find_recent_files(dir_config.source, max_age_days=scan_days_num))
            result.files_from_scan = len(scan_files)
            log.info(f"Stage 2: {len(scan_files)} files from dir scan (mtime < {scan_max_age_str})",
                     extra={"directory": dir_config.name, "file_count": len(scan_files)})

        # ── Stage 3: Merge + deduplicate ──
        all_candidates = sorted(fswatch_files | scan_files)
        result.files_merged = len(all_candidates)

        if include_dir_scan and len(scan_files) > 0:
            missed = len(scan_files - fswatch_files)
            coverage = len(fswatch_files & scan_files) / max(len(scan_files), 1) * 100
            result.fswatch_coverage = f"{coverage:.0f}%"
            log.info(f"Stage 3: Merged {len(all_candidates)} candidates. "
                     f"fswatch coverage: {coverage:.0f}% ({missed} caught by scan only)",
                     extra={"directory": dir_config.name})

        if not all_candidates:
            result.status = "skipped"
            result.finished_at = datetime.now().isoformat()
            result.duration_seconds = (datetime.now() - start_time).total_seconds()
            log.info(f"No changes detected for '{dir_config.name}' — skipping",
                     extra={"directory": dir_config.name})
            return result

        # ── Stage 4: Size + mtime filter ──
        size_changed, new_files, size_errors = compare_size_mtime(
            config, dir_config, all_candidates)
        result.files_after_size_mtime = len(size_changed)
        result.files_new = len(new_files)

        log.info(f"Stage 4: Size+mtime filter: {len(size_changed)} changed, "
                 f"{len(new_files)} new, {len(all_candidates) - len(size_changed) - len(new_files)} unchanged (skipped)",
                 extra={"directory": dir_config.name})

        # ── Stage 5: Checksum only the survivors ──
        if config.sync.checksum and size_changed:
            differs, checksum_new, checksum_errors = compare_checksums(
                config, dir_config, size_changed)
            result.files_after_checksum = len(differs)

            log.info(f"Stage 5: Checksum: {len(differs)} confirmed changed, "
                     f"{len(size_changed) - len(differs)} false positives (mtime changed, content same)",
                     extra={"directory": dir_config.name})
        else:
            differs = size_changed
            result.files_after_checksum = len(differs)

        # Combine: confirmed changes + brand new files
        needs_sync = sorted(set(differs) | set(new_files))

        if not needs_sync:
            result.status = "skipped"
            result.finished_at = datetime.now().isoformat()
            result.duration_seconds = (datetime.now() - start_time).total_seconds()
            log.info(f"All files match for '{dir_config.name}' — nothing to sync",
                     extra={"directory": dir_config.name})
            return result

        # ── Stage 6: Sync ──
        result = _do_sync(config, dir_config, needs_sync, result, dry_run)

        # ── Stage 7: Report ──
        if generate_report:
            _save_report(config, result)

    except subprocess.TimeoutExpired:
        result.status = "failed"
        result.errors.append("Sync timed out")
    except Exception as e:
        result.status = "failed"
        result.errors.append(str(e))
        log.error(f"Sync failed for '{dir_config.name}': {e}",
                  extra={"directory": dir_config.name, "error": str(e)})

    result.finished_at = datetime.now().isoformat()
    result.duration_seconds = (datetime.now() - start_time).total_seconds()
    return result


# ── Full Sync (last resort) ─────────────────────────────────

def full_sync(config, dir_config, max_age="8d", dry_run=False):
    """Full sync: rclone copy --max-age directly. No fswatch, no hasher."""
    result = SyncResult(dir_name=dir_config.name, sync_mode="full",
                         dry_run=dry_run, started_at=datetime.now().isoformat())
    start_time = datetime.now()
    try:
        cmd = _build_copy_cmd(config, dir_config, dry_run=dry_run, max_age=max_age)
        log.info(f"Full sync '{dir_config.name}' with max_age={max_age}",
                 extra={"directory": dir_config.name})
        r = _run_rclone(cmd)
        result.status = "dry_run" if dry_run else ("success" if r.returncode == 0 else "failed")
        if r.returncode != 0: result.errors.append(r.stderr.strip())
    except subprocess.TimeoutExpired:
        result.status = "failed"; result.errors.append("Timeout")
    except Exception as e:
        result.status = "failed"; result.errors.append(str(e))
    result.finished_at = datetime.now().isoformat()
    result.duration_seconds = (datetime.now() - start_time).total_seconds()
    return result


# ── Do Sync (shared by smart_sync) ──────────────────────────

def _do_sync(config, dir_config, file_list, result, dry_run):
    with tempfile.TemporaryDirectory(prefix="cloudsync-sync-") as tmp:
        ff = _write_files_from(file_list, tmp)
        cmd = _build_copy_cmd(config, dir_config, files_from_path=ff, dry_run=dry_run)

        log.info(f"{'Dry run' if dry_run else 'Syncing'} {len(file_list)} files for '{dir_config.name}'",
                 extra={"directory": dir_config.name, "file_count": len(file_list)})

        r = _run_rclone(cmd)

        if r.returncode == 0:
            result.synced_files = file_list
            result.files_synced = len(file_list)
            if dry_run:
                result.status = "dry_run"
            else:
                vr = _run_rclone(_build_verify_cmd(config, dir_config, ff), timeout=600)
                result.status = "success" if vr.returncode == 0 else "partial"
                if vr.returncode != 0:
                    result.errors.append("Post-sync verification found differences")
        else:
            result.status = "failed"
            result.errors.append(r.stderr.strip())
    return result


# ── Multi-directory Sync ─────────────────────────────────────

def sync_directory(config, dir_name, mode="smart", dry_run=False, date=None,
                   scan_days=1, include_dir_scan=False, generate_report=False):
    dc = next((d for d in config.directories if d.name == dir_name), None)
    if dc is None:
        r = SyncResult(dir_name=dir_name, status="failed", sync_mode=mode)
        r.errors.append(f"Directory '{dir_name}' not found in config")
        return r
    if mode == "smart":
        return smart_sync(config, dc, dry_run=dry_run, date=date,
                          scan_days=scan_days, include_dir_scan=include_dir_scan,
                          generate_report=generate_report)
    elif mode == "full":
        return full_sync(config, dc, max_age=f"{config.schedule.sync_schedule.scan_days}d", dry_run=dry_run)
    else:
        r = SyncResult(dir_name=dir_name, status="failed", sync_mode=mode)
        r.errors.append(f"Unknown mode: {mode}")
        return r


def sync_all(config, mode="smart", dry_run=False, **kwargs):
    results = {}
    dirs = config.directories if mode == "full" else get_watched_directories(config)
    for dc in dirs:
        results[dc.name] = sync_directory(config, dc.name, mode=mode, dry_run=dry_run, **kwargs)
    return results


# ── Diff (Preview) ───────────────────────────────────────────

def diff_directory(config, dir_name, scan_days=1, include_dir_scan=False):
    dc = next((d for d in config.directories if d.name == dir_name), None)
    if dc is None: return {"error": f"Directory '{dir_name}' not found"}

    if scan_days > 1:
        fswatch_files = set(get_changed_files_range(config, dc.name, days=scan_days))
    else:
        fswatch_files = set(get_changed_files(config, dc.name))

    scan_files = set()
    if include_dir_scan:
        days = config.schedule.sync_schedule.scan_days
        scan_files = set(find_recent_files(dc.source, max_age_days=days))

    merged = sorted(fswatch_files | scan_files)

    if not merged:
        return {"dir_name": dir_name, "files_from_fswatch": 0, "files_from_scan": 0,
                "merged": 0, "size_mtime_changed": 0, "checksum_differs": 0,
                "new_files": 0, "would_sync": 0}

    size_changed, new_files, _ = compare_size_mtime(config, dc, merged)

    differs = size_changed
    if config.sync.checksum and size_changed:
        differs, _, _ = compare_checksums(config, dc, size_changed)

    return {
        "dir_name": dir_name,
        "files_from_fswatch": len(fswatch_files),
        "files_from_scan": len(scan_files),
        "fswatch_missed": len(scan_files - fswatch_files) if scan_files else 0,
        "merged": len(merged),
        "size_mtime_changed": len(size_changed),
        "checksum_differs": len(differs),
        "new_files": len(new_files),
        "would_sync": len(set(differs) | set(new_files)),
    }


# ── Reporting ────────────────────────────────────────────────

def _save_report(config, result):
    log_dir = Path(config.project.log_dir) / "changelogs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    report_file = log_dir / f"report-{result.dir_name}-{ts}.json"
    report_file.write_text(json.dumps(result.to_dict(), indent=2))
    log.info(f"Report saved: {report_file}", extra={"directory": result.dir_name})


def save_sync_log(config, result):
    log_dir = Path(config.project.log_dir) / "sync"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    (log_dir / f"sync-{result.dir_name}-{ts}.json").write_text(
        json.dumps(result.to_dict(), indent=2))
