"""
tests/test_syncer.py — Phase 3 tests for merged smart sync pipeline.
Tests: hasher (size+mtime, checksum), syncer (smart, full, diff), watcher (range, scan)
"""
import json, os, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest, yaml

from cloudsync.config import load_config, DirectoryConfig
from cloudsync.hasher import (compare_checksums, compare_size_mtime, compare_sizes_only,
                               full_integrity_check, _build_remote_path, _write_files_from, _read_file_list)
from cloudsync.syncer import (SyncResult, smart_sync, full_sync, sync_directory, sync_all,
                               diff_directory, save_sync_log, _build_copy_cmd, _build_verify_cmd)
from cloudsync.watcher import (get_changed_files, get_changed_files_range, find_recent_files, _event_log)
from cloudsync.logger import ensure_log_dirs


@pytest.fixture
def test_config(tmp_path):
    for d in ("source_a", "source_b", "source_static"):
        (tmp_path / d).mkdir()
    (tmp_path / "logs").mkdir()
    cfg = {
        "project": {"name": "test", "log_dir": str(tmp_path / "logs")},
        "remote": {"name": "myremote", "type": "s3", "provider": "AWS",
                    "region": "ap-south-1", "bucket": "test-bucket"},
        "directories": [
            {"name": "dir_a", "source": str(tmp_path / "source_a"), "dest": "data/dir_a",
             "watch": True, "exclude": ["*.tmp"]},
            {"name": "dir_b", "source": str(tmp_path / "source_b"), "dest": "data/dir_b", "watch": True},
            {"name": "dir_static", "source": str(tmp_path / "source_static"), "dest": "data/static", "watch": False},
        ],
        "sync": {"transfers": 16, "checkers": 16, "checksum": True, "backup_versions": True, "version_dir": "versions"},
        "schedule": {"sync_schedule": {"scan_days": 8, "include_dir_scan": True, "generate_report": True}},
    }
    f = tmp_path / "test.yaml"
    f.write_text(yaml.dump(cfg))
    c = load_config(str(f), check_paths=True)
    ensure_log_dirs(c)
    return c

@pytest.fixture
def dir_config_a(test_config): return test_config.directories[0]

def _write_fswatch_log(config, dir_name, date, content):
    lf = Path(config.project.log_dir) / "fswatch" / f"{dir_name}-{date}.log"
    lf.write_text(content)


# ── Hasher: Helpers ──────────────────────────────────────────

class TestRemotePath:
    def test_builds(self, test_config, dir_config_a):
        assert _build_remote_path(test_config, dir_config_a) == "myremote:test-bucket/data/dir_a"

class TestFilesFrom:
    def test_writes(self):
        with tempfile.TemporaryDirectory() as t:
            p = _write_files_from(["a.py", "b.py"], t)
            assert "a.py" in Path(p).read_text()

class TestReadFileList:
    def test_reads(self, tmp_path):
        f = tmp_path / "f.txt"; f.write_text("a\nb\n")
        assert _read_file_list(f) == ["a", "b"]
    def test_skips_empty(self, tmp_path):
        f = tmp_path / "f.txt"; f.write_text("a\n\nb\n")
        assert _read_file_list(f) == ["a", "b"]
    def test_missing(self, tmp_path):
        assert _read_file_list(tmp_path / "nope") == []


# ── Hasher: Size + mtime ────────────────────────────────────

class TestSizeMtime:
    @patch("cloudsync.hasher.subprocess.run")
    def test_calls_size_only(self, mock_run, test_config, dir_config_a):
        mock_run.return_value = MagicMock(returncode=0)
        compare_size_mtime(test_config, dir_config_a, ["f.py"])
        cmd = mock_run.call_args[0][0]
        assert "--size-only" in cmd
        assert "--checksum" not in cmd

    @patch("cloudsync.hasher.subprocess.run")
    def test_empty_list(self, mock_run, test_config, dir_config_a):
        r = compare_size_mtime(test_config, dir_config_a, [])
        assert r == ([], [], [])
        mock_run.assert_not_called()

    def test_backward_compat(self, test_config, dir_config_a):
        """compare_sizes_only is an alias for compare_size_mtime."""
        with patch("cloudsync.hasher.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            compare_sizes_only(test_config, dir_config_a, ["f.py"])
            cmd = m.call_args[0][0]
            assert "--size-only" in cmd


# ── Hasher: Checksum ─────────────────────────────────────────

class TestChecksum:
    @patch("cloudsync.hasher.subprocess.run")
    def test_calls_checksum(self, mock_run, test_config, dir_config_a):
        mock_run.return_value = MagicMock(returncode=0)
        compare_checksums(test_config, dir_config_a, ["f.py"])
        cmd = mock_run.call_args[0][0]
        assert "--checksum" in cmd

    @patch("cloudsync.hasher.subprocess.run")
    def test_empty(self, mock_run, test_config, dir_config_a):
        assert compare_checksums(test_config, dir_config_a, []) == ([], [], [])

    @patch("cloudsync.hasher.subprocess.run")
    def test_parses_results(self, mock_run, test_config, dir_config_a):
        def se(cmd, **kw):
            for i, a in enumerate(cmd):
                if a == "--differ" and i+1 < len(cmd): Path(cmd[i+1]).write_text("changed.py\n")
                if a == "--missing-on-dst" and i+1 < len(cmd): Path(cmd[i+1]).write_text("new.py\n")
                if a == "--error" and i+1 < len(cmd): Path(cmd[i+1]).write_text("")
            return MagicMock(returncode=1, stderr="")
        mock_run.side_effect = se
        d, m, e = compare_checksums(test_config, dir_config_a, ["changed.py", "new.py"])
        assert "changed.py" in d and "new.py" in m


# ── Watcher: Range + Scan ───────────────────────────────────

class TestChangedFilesRange:
    def test_reads_multiple_days(self, test_config):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_fswatch_log(test_config, "dir_a", today, "t Updated    a.py\n")
        _write_fswatch_log(test_config, "dir_a", yesterday, "t Created    b.py\n")
        files = get_changed_files_range(test_config, "dir_a", days=2)
        assert "a.py" in files and "b.py" in files

    def test_deduplicates_across_days(self, test_config):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_fswatch_log(test_config, "dir_a", today, "t Updated    same.py\n")
        _write_fswatch_log(test_config, "dir_a", yesterday, "t Updated    same.py\n")
        files = get_changed_files_range(test_config, "dir_a", days=2)
        assert files.count("same.py") == 1

    def test_no_logs(self, test_config):
        assert get_changed_files_range(test_config, "dir_a", days=7) == []

class TestFindRecentFiles:
    def test_finds_recent(self, tmp_path):
        (tmp_path / "new.txt").write_text("new")
        files = find_recent_files(str(tmp_path), max_age_days=1)
        assert "new.txt" in files

    def test_returns_relative(self, tmp_path):
        sub = tmp_path / "sub"; sub.mkdir()
        (sub / "file.py").write_text("x")
        files = find_recent_files(str(tmp_path), max_age_days=1)
        assert "sub/file.py" in files

    def test_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"; empty.mkdir()
        assert find_recent_files(str(empty), max_age_days=1) == []


# ── Syncer: Command Building ─────────────────────────────────

class TestCopyCmd:
    def test_basic(self, test_config, dir_config_a):
        cmd = _build_copy_cmd(test_config, dir_config_a)
        assert cmd[:2] == ["rclone", "copy"]
        assert "--transfers" in cmd

    def test_checksum(self, test_config, dir_config_a):
        assert "--checksum" in _build_copy_cmd(test_config, dir_config_a)

    def test_dry_run(self, test_config, dir_config_a):
        assert "--dry-run" in _build_copy_cmd(test_config, dir_config_a, dry_run=True)

    def test_files_from(self, test_config, dir_config_a):
        cmd = _build_copy_cmd(test_config, dir_config_a, files_from_path="/tmp/f.txt")
        i = cmd.index("--files-from"); assert cmd[i+1] == "/tmp/f.txt"

    def test_max_age(self, test_config, dir_config_a):
        cmd = _build_copy_cmd(test_config, dir_config_a, max_age="8d")
        i = cmd.index("--max-age"); assert cmd[i+1] == "8d"

    def test_excludes(self, test_config, dir_config_a):
        cmd = _build_copy_cmd(test_config, dir_config_a)
        i = cmd.index("--exclude"); assert cmd[i+1] == "*.tmp"

    def test_backup_dir(self, test_config, dir_config_a):
        assert "--backup-dir" in _build_copy_cmd(test_config, dir_config_a)

    def test_no_backup_on_dry(self, test_config, dir_config_a):
        assert "--backup-dir" not in _build_copy_cmd(test_config, dir_config_a, dry_run=True)

class TestVerifyCmd:
    def test_size_only(self, test_config, dir_config_a):
        cmd = _build_verify_cmd(test_config, dir_config_a, "/tmp/f.txt")
        assert "--size-only" in cmd and "--one-way" in cmd


# ── Syncer: SyncResult ──────────────────────────────────────

class TestSyncResult:
    def test_defaults(self):
        r = SyncResult(dir_name="x"); assert r.status == "pending" and r.errors == []
    def test_to_dict(self):
        r = SyncResult(dir_name="x", status="success", files_synced=5)
        d = r.to_dict(); assert d["files_synced"] == 5


# ── Syncer: Smart Sync Pipeline ─────────────────────────────

class TestSmartSync:
    @patch("cloudsync.syncer._run_rclone")
    @patch("cloudsync.syncer.compare_checksums")
    @patch("cloudsync.syncer.compare_size_mtime")
    def test_no_changes_skips(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        result = smart_sync(test_config, dir_config_a)
        assert result.status == "skipped"
        mock_sm.assert_not_called()

    @patch("cloudsync.syncer._run_rclone")
    @patch("cloudsync.syncer.compare_checksums")
    @patch("cloudsync.syncer.compare_size_mtime", return_value=([], [], []))
    def test_size_mtime_all_match(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\n")
        result = smart_sync(test_config, dir_config_a)
        assert result.status == "skipped"
        mock_cs.assert_not_called()  # checksum never called

    @patch("cloudsync.syncer._run_rclone")
    @patch("cloudsync.syncer.compare_checksums", return_value=([], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_checksum_all_match(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\n")
        result = smart_sync(test_config, dir_config_a)
        assert result.status == "skipped"  # size changed but checksum same
        mock_rc.assert_not_called()

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], ["b.py"], []))
    def test_full_pipeline_syncs(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\nt Created    b.py\n")
        result = smart_sync(test_config, dir_config_a)
        assert result.status == "success"
        assert result.files_from_fswatch == 2
        assert result.files_after_size_mtime == 1
        assert result.files_new == 1
        assert result.files_synced == 2  # a.py (checksum diff) + b.py (new)

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_dry_run(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\n")
        result = smart_sync(test_config, dir_config_a, dry_run=True)
        assert result.status == "dry_run" and result.dry_run is True

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=1, stderr="fail"))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_failure(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\n")
        result = smart_sync(test_config, dir_config_a)
        assert result.status == "failed"

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_weekly_with_scan(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        """Weekly mode: multi-day logs + dir scan."""
        today = datetime.now().strftime("%Y-%m-%d")
        _write_fswatch_log(test_config, "dir_a", today, "t Updated    a.py\n")
        with patch("cloudsync.syncer.find_recent_files", return_value=["a.py", "missed.py"]):
            result = smart_sync(test_config, dir_config_a,
                               scan_days=8, include_dir_scan=True)
        assert result.files_from_fswatch == 1
        assert result.files_from_scan == 2
        assert result.files_merged == 2  # a.py + missed.py

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_generates_report(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    a.py\n")
        result = smart_sync(test_config, dir_config_a, generate_report=True)
        reports = list((Path(test_config.project.log_dir) / "changelogs").glob("report-*.json"))
        assert len(reports) >= 1

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    @patch("cloudsync.syncer.compare_checksums", return_value=(["a.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["a.py"], [], []))
    def test_fswatch_coverage_stat(self, mock_sm, mock_cs, mock_rc, test_config, dir_config_a):
        today = datetime.now().strftime("%Y-%m-%d")
        _write_fswatch_log(test_config, "dir_a", today, "t Updated    a.py\n")
        with patch("cloudsync.syncer.find_recent_files", return_value=["a.py", "missed.py"]):
            result = smart_sync(test_config, dir_config_a,
                               scan_days=1, include_dir_scan=True)
        assert result.fswatch_coverage  # should have a percentage


# ── Syncer: Full Sync ───────────────────────────────────────

class TestFullSync:
    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    def test_success(self, mock_rc, test_config, dir_config_a):
        r = full_sync(test_config, dir_config_a, max_age="8d")
        assert r.status == "success" and r.sync_mode == "full"
        assert "--max-age" in mock_rc.call_args[0][0]

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=0, stderr=""))
    def test_dry_run(self, mock_rc, test_config, dir_config_a):
        r = full_sync(test_config, dir_config_a, dry_run=True)
        assert r.status == "dry_run"

    @patch("cloudsync.syncer._run_rclone", return_value=MagicMock(returncode=1, stderr="err"))
    def test_failure(self, mock_rc, test_config, dir_config_a):
        assert full_sync(test_config, dir_config_a).status == "failed"


# ── Syncer: Routing ──────────────────────────────────────────

class TestSyncDirectory:
    @patch("cloudsync.syncer.smart_sync", return_value=SyncResult("x", "success"))
    def test_smart(self, m, test_config): sync_directory(test_config, "dir_a"); m.assert_called_once()

    @patch("cloudsync.syncer.full_sync", return_value=SyncResult("x", "success"))
    def test_full(self, m, test_config): sync_directory(test_config, "dir_a", mode="full"); m.assert_called_once()

    def test_unknown_dir(self, test_config):
        assert sync_directory(test_config, "nope").status == "failed"

    def test_unknown_mode(self, test_config):
        assert sync_directory(test_config, "dir_a", mode="bad").status == "failed"

class TestSyncAll:
    @patch("cloudsync.syncer.sync_directory", return_value=SyncResult("x", "success"))
    def test_smart_watched_only(self, m, test_config):
        r = sync_all(test_config, mode="smart")
        assert "dir_a" in r and "dir_static" not in r

    @patch("cloudsync.syncer.sync_directory", return_value=SyncResult("x", "success"))
    def test_full_all(self, m, test_config):
        r = sync_all(test_config, mode="full")
        assert "dir_static" in r


# ── Syncer: Diff ─────────────────────────────────────────────

class TestDiff:
    def test_no_changes(self, test_config):
        r = diff_directory(test_config, "dir_a")
        assert r["would_sync"] == 0

    @patch("cloudsync.syncer.compare_checksums", return_value=(["x.py"], [], []))
    @patch("cloudsync.syncer.compare_size_mtime", return_value=(["x.py"], ["y.py"], []))
    def test_with_changes(self, mock_sm, mock_cs, test_config):
        _write_fswatch_log(test_config, "dir_a", datetime.now().strftime("%Y-%m-%d"),
                           "t Updated    x.py\nt Created    y.py\n")
        r = diff_directory(test_config, "dir_a")
        assert r["would_sync"] == 2

    def test_unknown_dir(self, test_config):
        assert "error" in diff_directory(test_config, "nope")


# ── Syncer: Save Log ─────────────────────────────────────────

class TestSaveLog:
    def test_saves(self, test_config):
        save_sync_log(test_config, SyncResult("dir_a", "success", files_synced=5))
        logs = list((Path(test_config.project.log_dir) / "sync").glob("sync-*.json"))
        assert len(logs) == 1
        assert json.loads(logs[0].read_text())["files_synced"] == 5
