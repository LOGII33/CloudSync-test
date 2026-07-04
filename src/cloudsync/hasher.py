"""
cloudsync.hasher — Checksum comparison engine.

Three comparison levels:
  compare_size_mtime()  — fastest, metadata only (Stage 1 filter)
  compare_checksums()   — accurate, reads file content (Stage 2 verify)
  full_integrity_check() — comprehensive, all files (monthly audit)

Production pipeline: size+mtime first → checksum only survivors
"""
from __future__ import annotations
import subprocess, tempfile
from pathlib import Path
from typing import List, Optional, Tuple
from cloudsync.config import CloudSyncConfig, DirectoryConfig
from cloudsync.logger import get_logger
log = get_logger("hasher")


def _build_remote_path(config: CloudSyncConfig, dir_config: DirectoryConfig) -> str:
    return f"{config.remote.name}:{config.remote.bucket}/{dir_config.dest}"


def _write_files_from(file_list: List[str], tmp_dir: str) -> str:
    path = Path(tmp_dir) / "files-from.txt"
    path.write_text("\n".join(file_list) + "\n")
    return str(path)


def _read_file_list(path: Path) -> List[str]:
    if not path.exists(): return []
    return [l.strip() for l in path.read_text().split("\n") if l.strip()]


def _run_check(
    source: str,
    remote_path: str,
    files_from_path: Optional[str],
    use_checksum: bool,
    differ_path: Path,
    missing_path: Path,
    error_path: Path,
    extra_flags: List[str] = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess:
    """Core rclone check runner used by all comparison functions."""
    cmd = ["rclone", "check", source, remote_path, "--one-way",
           "--differ", str(differ_path), "--missing-on-dst", str(missing_path),
           "--error", str(error_path)]

    if files_from_path:
        cmd.extend(["--files-from", files_from_path])

    if use_checksum:
        cmd.append("--checksum")
    else:
        cmd.append("--size-only")

    if extra_flags:
        cmd.extend(extra_flags)

    log.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def compare_size_mtime(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    file_list: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """
    Stage 1: Compare files by size + modification time only.
    Fast — no file content reading. One HEAD request per file.

    Returns (changed, missing_on_dest, errors):
      - changed: files with different size or mtime
      - missing_on_dest: files not in S3
      - errors: files with comparison errors
    """
    if not file_list:
        return [], [], []

    remote_path = _build_remote_path(config, dir_config)

    with tempfile.TemporaryDirectory(prefix="cloudsync-") as tmp:
        ff = _write_files_from(file_list, tmp)
        dp = Path(tmp) / "differ.txt"
        mp = Path(tmp) / "missing.txt"
        ep = Path(tmp) / "errors.txt"

        log.info(f"Size+mtime check: {len(file_list)} files for '{dir_config.name}'",
                 extra={"directory": dir_config.name, "file_count": len(file_list)})

        _run_check(dir_config.source, remote_path, ff, use_checksum=False, differ_path=dp, missing_path=mp, error_path=ep)

        changed = _read_file_list(dp)
        missing = _read_file_list(mp)
        errors = _read_file_list(ep)

        log.info(f"Size+mtime result: {len(changed)} changed, {len(missing)} missing, {len(errors)} errors",
                 extra={"directory": dir_config.name})

        return changed, missing, errors


def compare_checksums(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    file_list: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """
    Stage 2: Compare files by MD5 checksum.
    Reads entire file content. Use only for files that passed Stage 1.

    Returns (differs, missing_on_dest, errors).
    """
    if not file_list:
        return [], [], []

    remote_path = _build_remote_path(config, dir_config)

    with tempfile.TemporaryDirectory(prefix="cloudsync-") as tmp:
        ff = _write_files_from(file_list, tmp)
        dp = Path(tmp) / "differ.txt"
        mp = Path(tmp) / "missing.txt"
        ep = Path(tmp) / "errors.txt"

        log.info(f"Checksum compare: {len(file_list)} files for '{dir_config.name}'",
                 extra={"directory": dir_config.name, "file_count": len(file_list)})

        result = _run_check(dir_config.source, remote_path, ff, use_checksum=True, differ_path=dp, missing_path=mp, error_path=ep)

        if result.returncode not in (0, 1):
            log.error(f"rclone check failed: {result.stderr.strip()}",
                      extra={"directory": dir_config.name, "error": result.stderr.strip()})

        differs = _read_file_list(dp)
        missing = _read_file_list(mp)
        errors = _read_file_list(ep)

        log.info(f"Checksum result: {len(differs)} differ, {len(missing)} missing",
                 extra={"directory": dir_config.name})

        return differs, missing, errors


def compare_sizes_only(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    file_list: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """Alias for compare_size_mtime for backward compatibility."""
    return compare_size_mtime(config, dir_config, file_list)


def full_integrity_check(
    config: CloudSyncConfig,
    dir_config: DirectoryConfig,
    use_checksum: bool = False,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Full integrity check of ALL files (no --files-from).
    Used by monthly audit.
    """
    remote_path = _build_remote_path(config, dir_config)

    with tempfile.TemporaryDirectory(prefix="cloudsync-") as tmp:
        dp = Path(tmp) / "differ.txt"
        mp = Path(tmp) / "missing.txt"
        ep = Path(tmp) / "errors.txt"

        log.info(f"Full integrity check for '{dir_config.name}' (checksum={use_checksum})",
                 extra={"directory": dir_config.name})

        _run_check(dir_config.source, remote_path, files_from_path=None,
                   use_checksum=use_checksum, differ_path=dp, missing_path=mp,
                   error_path=ep, extra_flags=["--fast-list"], timeout=7200)

        return _read_file_list(dp), _read_file_list(mp), _read_file_list(ep)
