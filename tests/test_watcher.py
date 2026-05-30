"""
tests/test_watcher.py — Phase 2 tests for fswatch watcher management.

Run with: pytest tests/test_watcher.py -v
Run all:  pytest tests/ -v
"""

import json
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest
import yaml

from cloudsync.config import load_config, CloudSyncConfig, DirectoryConfig
from cloudsync.watcher import (
    # Data structures
    WatcherMetadata,
    FswatchEvent,
    SystemInfo,
    # Constants
    MONITOR_PRIORITY,
    INOTIFY_RECOMMENDED,
    # OS detection
    detect_monitor,
    _get_available_monitors,
    _read_inotify_limit,
    # Public API
    get_watched_directories,
    start_watcher,
    stop_watcher,
    start_all,
    stop_all,
    get_status,
    read_events,
    get_changed_files,
    # Internals
    _build_fswatch_cmd,
    _parse_fswatch_line,
    _pid_file,
    _meta_file,
    _event_log,
    _save_metadata,
    _load_metadata,
    _is_process_alive,
)
from cloudsync.logger import setup_logging, ensure_log_dirs


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def test_config(tmp_path):
    """Create a test config with real tmp directories."""
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    source_static = tmp_path / "source_static"
    source_a.mkdir()
    source_b.mkdir()
    source_static.mkdir()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    config_dict = {
        "project": {"name": "test", "log_dir": str(log_dir)},
        "remote": {"name": "test-remote", "type": "s3", "provider": "AWS",
                    "region": "us-east-1", "bucket": "test-bucket"},
        "directories": [
            {"name": "dir_a", "source": str(source_a), "dest": "a", "watch": True,
             "exclude": ["*.tmp", "__pycache__/**"]},
            {"name": "dir_b", "source": str(source_b), "dest": "b", "watch": True},
            {"name": "dir_static", "source": str(source_static), "dest": "static", "watch": False},
        ],
        "sync": {"debounce_seconds": 5},
    }

    config_file = tmp_path / "test.yaml"
    config_file.write_text(yaml.dump(config_dict))
    config = load_config(str(config_file), check_paths=True)
    ensure_log_dirs(config)
    return config


@pytest.fixture
def sample_dir_config(tmp_path):
    """Single DirectoryConfig for unit tests."""
    source = tmp_path / "sample"
    source.mkdir()
    return DirectoryConfig(
        name="sample",
        source=str(source),
        dest="sample",
        watch=True,
        exclude=["*.tmp", "*.log"],
    )


# ── SystemInfo / detect_monitor Tests ───────────────────────

class TestDetectMonitor:
    """detect_monitor() picks the right driver per OS."""

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Linux")
    def test_linux_picks_inotify(self, mock_sys, mock_avail):
        mock_avail.return_value = ["inotify_monitor", "poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "inotify_monitor"
        assert info.os_name == "Linux"
        assert not info.fallback_used

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Darwin")
    def test_macos_picks_fsevents(self, mock_sys, mock_avail):
        mock_avail.return_value = ["fsevents_monitor", "kqueue_monitor", "poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "fsevents_monitor"
        assert not info.fallback_used

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Darwin")
    def test_macos_fallback_to_kqueue(self, mock_sys, mock_avail):
        # fsevents not available (e.g. older macOS / cross-compile)
        mock_avail.return_value = ["kqueue_monitor", "poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "kqueue_monitor"

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="FreeBSD")
    def test_bsd_picks_kqueue(self, mock_sys, mock_avail):
        mock_avail.return_value = ["kqueue_monitor", "poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "kqueue_monitor"
        assert not info.fallback_used

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Linux")
    def test_linux_fallback_to_poll(self, mock_sys, mock_avail):
        # inotify not available
        mock_avail.return_value = ["poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "poll_monitor"
        assert info.fallback_used

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Linux")
    def test_no_fswatch_installed(self, mock_sys, mock_avail):
        mock_avail.return_value = []
        info = detect_monitor()
        assert info.selected_monitor == "poll_monitor"
        assert info.fallback_used
        assert len(info.warnings) >= 1
        assert "fswatch" in info.warnings[0].lower()

    @patch("cloudsync.watcher._read_inotify_limit")
    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="Linux")
    def test_linux_low_inotify_watches_warns(self, mock_sys, mock_avail, mock_limit):
        mock_avail.return_value = ["inotify_monitor", "poll_monitor"]
        # Return a value below the recommended threshold
        mock_limit.side_effect = lambda key: (
            1024 if key == "max_user_watches" else INOTIFY_RECOMMENDED["max_queued_events"]
        )
        info = detect_monitor()
        assert any("max_user_watches" in w for w in info.warnings)
        assert any("sysctl" in w for w in info.warnings)

    @patch("cloudsync.watcher._get_available_monitors")
    @patch("platform.system", return_value="SunOS")
    def test_unknown_os_falls_back_to_poll(self, mock_sys, mock_avail):
        mock_avail.return_value = ["poll_monitor"]
        info = detect_monitor()
        assert info.selected_monitor == "poll_monitor"
        assert info.fallback_used


class TestMonitorPriority:
    """MONITOR_PRIORITY always ends with poll_monitor as last resort."""

    def test_all_os_have_poll_as_last(self):
        for os_name, priority in MONITOR_PRIORITY.items():
            assert priority[-1] == "poll_monitor", (
                f"{os_name} priority list does not end with poll_monitor: {priority}"
            )

    def test_linux_prefers_inotify(self):
        assert MONITOR_PRIORITY["Linux"][0] == "inotify_monitor"

    def test_darwin_prefers_fsevents(self):
        assert MONITOR_PRIORITY["Darwin"][0] == "fsevents_monitor"


# ── WatcherMetadata Tests ────────────────────────────────────

class TestWatcherMetadata:
    def test_create_default(self):
        meta = WatcherMetadata("test_dir", "/path/to/source")
        assert meta.dir_name == "test_dir"
        assert meta.status == "stopped"
        assert meta.pid is None
        assert meta.event_count == 0
        assert meta.monitor is None

    def test_to_dict(self):
        meta = WatcherMetadata("test_dir", "/path")
        meta.pid = 12345
        meta.status = "running"
        meta.started_at = "2026-04-29T10:00:00"
        meta.monitor = "inotify_monitor"
        d = meta.to_dict()
        assert d["pid"] == 12345
        assert d["status"] == "running"
        assert d["dir_name"] == "test_dir"
        assert d["monitor"] == "inotify_monitor"

    def test_roundtrip(self):
        meta = WatcherMetadata("test_dir", "/path")
        meta.pid = 99
        meta.event_count = 42
        meta.status = "running"
        meta.last_event_at = "2026-04-29T14:00:00"
        meta.last_event_path = "models/train.py"
        meta.monitor = "fsevents_monitor"

        restored = WatcherMetadata.from_dict(meta.to_dict())
        assert restored.pid == 99
        assert restored.event_count == 42
        assert restored.status == "running"
        assert restored.last_event_path == "models/train.py"
        assert restored.monitor == "fsevents_monitor"

    def test_roundtrip_no_monitor(self):
        """Older .meta.json files without 'monitor' key load cleanly."""
        meta = WatcherMetadata("test_dir", "/path")
        meta.pid = 10
        d = meta.to_dict()
        del d["monitor"]  # simulate old metadata file
        restored = WatcherMetadata.from_dict(d)
        assert restored.monitor is None

    def test_save_and_load(self, test_config):
        meta = WatcherMetadata("dir_a", "/path")
        meta.pid = 555
        meta.status = "running"
        meta.monitor = "inotify_monitor"
        _save_metadata(test_config, meta)

        loaded = _load_metadata(test_config, "dir_a")
        assert loaded is not None
        assert loaded.pid == 555
        assert loaded.status == "running"
        assert loaded.monitor == "inotify_monitor"

    def test_load_nonexistent(self, test_config):
        loaded = _load_metadata(test_config, "nonexistent")
        assert loaded is None

    def test_load_corrupted(self, test_config):
        meta_path = _meta_file(test_config, "dir_a")
        meta_path.write_text("not valid json{{{")
        loaded = _load_metadata(test_config, "dir_a")
        assert loaded is None


# ── FswatchEvent Tests ───────────────────────────────────────

class TestFswatchEvent:
    def test_create(self):
        event = FswatchEvent("2026-04-29T14:00:00", "models/train.py", "Created", "AIML")
        assert event.path == "models/train.py"
        assert event.event_type == "Created"

    def test_to_log_line(self):
        event = FswatchEvent("2026-04-29T14:00:00", "app.py", "Updated", "Projects")
        line = event.to_log_line()
        assert "2026-04-29T14:00:00" in line
        assert "Updated" in line
        assert "app.py" in line

    def test_to_dict(self):
        event = FswatchEvent("2026-04-29T14:00:00", "file.txt", "Removed", "docs")
        d = event.to_dict()
        assert d["event_type"] == "Removed"
        assert d["dir_name"] == "docs"


# ── fswatch Command Building ─────────────────────────────────

class TestBuildCommandWithMonitor:
    def test_native_monitor_included(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config, monitor="inotify_monitor")
        assert "--monitor" in cmd
        assert cmd[cmd.index("--monitor") + 1] == "inotify_monitor"

    def test_fsevents_monitor_included(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config, monitor="fsevents_monitor")
        assert "--monitor" in cmd
        assert cmd[cmd.index("--monitor") + 1] == "fsevents_monitor"

    def test_poll_monitor_not_explicitly_passed(self, sample_dir_config):
        """poll_monitor is fswatch's own default — no --monitor flag needed."""
        cmd = _build_fswatch_cmd(sample_dir_config, monitor="poll_monitor")
        assert "--monitor" not in cmd

    def test_no_monitor_arg_omits_flag(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config, monitor=None)
        assert "--monitor" not in cmd

    def test_basic_flags_always_present(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config)
        assert cmd[0] == "fswatch"
        assert "--recursive" in cmd
        assert "--event-flags" in cmd
        assert "--timestamp" in cmd
        assert sample_dir_config.source == cmd[-1]

    def test_includes_events(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config)
        cmd_str = " ".join(cmd)
        for event in ("Created", "Updated", "Removed", "Renamed"):
            assert event in cmd_str

    def test_includes_excludes(self, sample_dir_config):
        cmd = _build_fswatch_cmd(sample_dir_config)
        assert "--exclude" in cmd
        exclude_idx = [i for i, c in enumerate(cmd) if c == "--exclude"]
        exclude_patterns = [cmd[i + 1] for i in exclude_idx]
        assert "*.tmp" in exclude_patterns
        assert "*.log" in exclude_patterns

    def test_no_excludes(self, tmp_path):
        dir_config = DirectoryConfig(name="x", source=str(tmp_path), dest="x", exclude=[])
        cmd = _build_fswatch_cmd(dir_config)
        assert "--exclude" not in cmd


# ── fswatch Line Parsing ─────────────────────────────────────

class TestParseLine:
    def test_parse_with_flags(self):
        line = "/home/user/Tamizh/AIML/train.py Created IsFile"
        event = _parse_fswatch_line(line, "AIML", "/home/user/Tamizh/AIML")
        assert event is not None
        assert event.event_type == "Created"
        assert event.dir_name == "AIML"

    def test_parse_updated(self):
        line = "/home/user/file.py Updated IsFile"
        event = _parse_fswatch_line(line, "test", "/home/user")
        assert event is not None
        assert event.event_type == "Updated"

    def test_parse_removed(self):
        line = "/home/user/old.txt Removed"
        event = _parse_fswatch_line(line, "test", "/home/user")
        assert event is not None
        assert event.event_type == "Removed"

    def test_parse_empty_line(self):
        event = _parse_fswatch_line("", "test", "/home")
        assert event is None

    def test_parse_whitespace_only(self):
        event = _parse_fswatch_line("   ", "test", "/home")
        assert event is None

    def test_strips_source_prefix(self):
        line = "/home/user/Tamizh/AIML/models/v3.py Updated IsFile"
        event = _parse_fswatch_line(line, "AIML", "/home/user/Tamizh/AIML")
        assert event is not None
        assert event.path == "models/v3.py"


# ── Path Helpers ─────────────────────────────────────────────

class TestPathHelpers:
    def test_pid_file_location(self, test_config):
        path = _pid_file(test_config, "dir_a")
        assert "pids" in str(path)
        assert "dir_a.pid" in str(path)

    def test_meta_file_location(self, test_config):
        path = _meta_file(test_config, "dir_a")
        assert "dir_a.meta.json" in str(path)

    def test_event_log_contains_date(self, test_config):
        path = _event_log(test_config, "dir_a")
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in str(path)
        assert "dir_a" in str(path)


# ── Directory Filtering ──────────────────────────────────────

class TestGetWatchedDirectories:
    def test_filters_watched_only(self, test_config):
        watched = get_watched_directories(test_config)
        names = [d.name for d in watched]
        assert "dir_a" in names
        assert "dir_b" in names
        assert "dir_static" not in names

    def test_count(self, test_config):
        watched = get_watched_directories(test_config)
        assert len(watched) == 2


# ── Process Alive Check ─────────────────────────────────────

class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        assert _is_process_alive(99999999) is False


# ── Start/Stop Watcher (with mocked fswatch) ────────────────

class TestStartWithSystemInfo:
    """start_watcher() passes the detected monitor into the fswatch command
    and persists it in metadata."""

    @patch("cloudsync.watcher.subprocess.Popen")
    def test_start_uses_monitor_from_system_info(self, mock_popen, test_config):
        mock_process = MagicMock()
        mock_process.pid = 42000
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        sys_info = SystemInfo(
            os_name="Linux",
            os_release="5.15",
            os_arch="x86_64",
            selected_monitor="inotify_monitor",
            available_monitors=["inotify_monitor", "poll_monitor"],
        )

        dir_config = test_config.directories[0]  # dir_a
        meta = start_watcher(test_config, dir_config, system_info=sys_info)

        # Monitor is set on the returned metadata object immediately
        assert meta.monitor == "inotify_monitor"

        # Wait briefly for the daemon log-writer thread to write the file
        # (it exits instantly with the mock, triggering the finally block)
        meta_path = _meta_file(test_config, "dir_a")
        for _ in range(20):
            if meta_path.exists():
                break
            time.sleep(0.05)

        loaded = _load_metadata(test_config, "dir_a")
        assert loaded is not None
        assert loaded.monitor == "inotify_monitor"

        # --monitor flag passed to Popen
        call_args = mock_popen.call_args[0][0]
        assert "--monitor" in call_args
        assert call_args[call_args.index("--monitor") + 1] == "inotify_monitor"

    @patch("cloudsync.watcher.subprocess.Popen")
    @patch("cloudsync.watcher.detect_monitor")
    def test_start_calls_detect_when_no_system_info(
        self, mock_detect, mock_popen, test_config
    ):
        """When system_info is omitted, detect_monitor() is called automatically."""
        mock_detect.return_value = SystemInfo(
            os_name="Darwin",
            os_release="23.0",
            os_arch="arm64",
            selected_monitor="fsevents_monitor",
        )
        mock_process = MagicMock()
        mock_process.pid = 1111
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        dir_config = test_config.directories[0]
        meta = start_watcher(test_config, dir_config)  # no system_info

        mock_detect.assert_called_once()
        assert meta.monitor == "fsevents_monitor"

    @patch("cloudsync.watcher.subprocess.Popen")
    @patch("cloudsync.watcher.detect_monitor")
    def test_start_all_calls_detect_exactly_once(
        self, mock_detect, mock_popen, test_config
    ):
        """start_all() must call detect_monitor() exactly once regardless of
        how many directories are watched."""
        mock_detect.return_value = SystemInfo(
            os_name="Linux",
            os_release="5.15",
            os_arch="x86_64",
            selected_monitor="inotify_monitor",
        )
        mock_process = MagicMock()
        mock_process.pid = 2000
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        results = start_all(test_config)

        mock_detect.assert_called_once()
        # Both watched dirs should have the same monitor
        assert results["dir_a"].monitor == "inotify_monitor"
        assert results["dir_b"].monitor == "inotify_monitor"


class TestStartStopWatcher:
    @patch("cloudsync.watcher._get_available_monitors", return_value=["inotify_monitor", "poll_monitor"])
    @patch("cloudsync.watcher.subprocess.Popen")
    def test_start_creates_pid_file(self, mock_popen, mock_avail, test_config):
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        dir_config = test_config.directories[0]  # dir_a
        meta = start_watcher(test_config, dir_config)

        assert meta.pid == 12345
        assert _pid_file(test_config, "dir_a").exists()
        assert int(_pid_file(test_config, "dir_a").read_text()) == 12345

    @patch("cloudsync.watcher._get_available_monitors", return_value=["inotify_monitor", "poll_monitor"])
    @patch("cloudsync.watcher.subprocess.Popen")
    def test_start_creates_metadata(self, mock_popen, mock_avail, test_config):
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        dir_config = test_config.directories[0]
        start_watcher(test_config, dir_config)

        loaded = _load_metadata(test_config, "dir_a")
        assert loaded is not None
        assert loaded.pid == 12345
        assert loaded.started_at is not None

    @patch("cloudsync.watcher.subprocess.Popen")
    @patch("cloudsync.watcher._is_process_alive", return_value=True)
    def test_start_rejects_duplicate(self, mock_alive, mock_popen, test_config):
        """Can't start a watcher if one is already running."""
        meta = WatcherMetadata("dir_a", test_config.directories[0].source)
        meta.pid = 99999
        meta.status = "running"
        _save_metadata(test_config, meta)

        dir_config = test_config.directories[0]
        with pytest.raises(RuntimeError, match="already running"):
            start_watcher(test_config, dir_config)

    def test_start_rejects_missing_source(self, test_config):
        dir_config = DirectoryConfig(
            name="missing", source="/nonexistent/path", dest="x", watch=True,
        )
        with pytest.raises(FileNotFoundError):
            start_watcher(test_config, dir_config)

    def test_stop_nonexistent(self, test_config):
        result = stop_watcher(test_config, "nonexistent")
        assert result is False

    @patch("cloudsync.watcher._is_process_alive", return_value=False)
    def test_stop_already_dead(self, mock_alive, test_config):
        """Stop returns False if process already dead."""
        pid_file = _pid_file(test_config, "dir_a")
        pid_file.write_text("12345")
        result = stop_watcher(test_config, "dir_a")
        assert result is False
        assert not pid_file.exists()  # Cleaned up


# ── Start/Stop All ───────────────────────────────────────────

class TestStartStopAll:
    @patch("cloudsync.watcher._get_available_monitors", return_value=["inotify_monitor", "poll_monitor"])
    @patch("cloudsync.watcher.subprocess.Popen")
    def test_start_all_returns_results(self, mock_popen, mock_avail, test_config):
        mock_process = MagicMock()
        mock_process.pid = 100
        mock_process.stdout.readline.return_value = ""
        mock_popen.return_value = mock_process

        results = start_all(test_config)
        assert "dir_a" in results
        assert "dir_b" in results
        assert "dir_static" not in results

    def test_stop_all_when_none_running(self, test_config):
        results = stop_all(test_config)
        assert "dir_a" in results
        assert results["dir_a"] is False


# ── Status ───────────────────────────────────────────────────

class TestGetStatus:
    def test_status_empty(self, test_config):
        statuses = get_status(test_config)
        assert "dir_a" in statuses
        assert statuses["dir_a"].status == "stopped"

    def test_status_with_saved_metadata(self, test_config):
        meta = WatcherMetadata("dir_a", test_config.directories[0].source)
        meta.pid = os.getpid()  # Use current PID so it appears alive
        meta.status = "running"
        meta.event_count = 42
        _save_metadata(test_config, meta)

        statuses = get_status(test_config)
        assert statuses["dir_a"].status == "running"
        assert statuses["dir_a"].event_count == 42

    @patch("cloudsync.watcher._is_process_alive", return_value=False)
    def test_status_detects_dead_process(self, mock_alive, test_config):
        meta = WatcherMetadata("dir_a", test_config.directories[0].source)
        meta.pid = 99999
        meta.status = "running"
        _save_metadata(test_config, meta)

        statuses = get_status(test_config)
        assert statuses["dir_a"].status == "dead"


# ── Log Reading ──────────────────────────────────────────────

class TestReadEvents:
    def test_read_empty(self, test_config):
        events = read_events(test_config, "dir_a")
        assert events == []

    def test_read_from_log_file(self, test_config):
        log_file = _event_log(test_config, "dir_a")
        log_file.write_text(
            "2026-04-29T14:00:00 Created    models/train.py\n"
            "2026-04-29T14:01:00 Updated    models/train.py\n"
            "2026-04-29T14:02:00 Created    utils/helper.py\n"
            "2026-04-29T14:03:00 Removed    temp/cache.pkl\n"
            "2026-04-29T14:04:00 Updated    models/eval.py\n"
        )

        events = read_events(test_config, "dir_a", tail=3)
        assert len(events) == 3
        assert "helper.py" in events[0]
        assert "cache.pkl" in events[1]
        assert "eval.py" in events[2]

    def test_read_all(self, test_config):
        log_file = _event_log(test_config, "dir_a")
        log_file.write_text("line1\nline2\nline3\n")

        events = read_events(test_config, "dir_a", tail=None)
        assert len(events) == 3

    def test_read_specific_date(self, test_config):
        log_dir = Path(test_config.project.log_dir) / "fswatch"
        (log_dir / "dir_a-2026-04-20.log").write_text("old_event Created old/file.py\n")

        events = read_events(test_config, "dir_a", date="2026-04-20")
        assert len(events) == 1
        assert "old/file.py" in events[0]


# ── Changed Files Extraction ────────────────────────────────

class TestGetChangedFiles:
    def test_deduplicates(self, test_config):
        log_file = _event_log(test_config, "dir_a")
        log_file.write_text(
            "2026-04-29T14:00:00 Created    models/train.py\n"
            "2026-04-29T14:01:00 Updated    models/train.py\n"  # same file again
            "2026-04-29T14:02:00 Created    utils/helper.py\n"
        )

        files = get_changed_files(test_config, "dir_a")
        assert len(files) == 2
        assert "models/train.py" in files
        assert "utils/helper.py" in files

    def test_sorted_output(self, test_config):
        log_file = _event_log(test_config, "dir_a")
        log_file.write_text(
            "2026-04-29T14:00:00 Created    z_file.py\n"
            "2026-04-29T14:01:00 Created    a_file.py\n"
            "2026-04-29T14:02:00 Created    m_file.py\n"
        )

        files = get_changed_files(test_config, "dir_a")
        assert files == ["a_file.py", "m_file.py", "z_file.py"]

    def test_empty_log(self, test_config):
        files = get_changed_files(test_config, "dir_a")
        assert files == []


# ── CLI Integration Tests ────────────────────────────────────

class TestWatcherCLI:
    def test_watch_status_command(self, test_config):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "status", "--config", test_config.config_path])
        assert result.exit_code == 0

    def test_watch_stop_nothing_running(self, test_config):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "stop", "--config", test_config.config_path])
        assert result.exit_code == 0
        assert "not running" in result.output.lower() or "was not" in result.output.lower()

    def test_watch_logs_no_events(self, test_config):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "logs", "--config", test_config.config_path,
                                      "--dir", "dir_a"])
        assert result.exit_code == 0
        assert "no events" in result.output.lower()
