"""
tests/test_config.py — Phase 1 tests for config loading and validation.

Run with: pytest tests/test_config.py -v
"""

import os
from pathlib import Path

import pytest
import yaml

from cloudsync.config import load_config, ConfigError, CloudSyncConfig, generate_template


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def valid_config_dict():
    """Minimal valid config as a Python dict."""
    return {
        "project": {
            "name": "test-project",
            "log_dir": "/tmp/cloudsync-test",
        },
        "remote": {
            "name": "mys3",
            "type": "s3",
            "provider": "AWS",
            "region": "ap-south-1",
            "bucket": "test-bucket",
        },
        "directories": [
            {
                "name": "data",
                "source": "/tmp/cloudsync-test-source",
                "dest": "data",
                "watch": True,
                "exclude": ["*.tmp"],
            },
        ],
    }


@pytest.fixture
def valid_config_file(valid_config_dict, tmp_path):
    """Write valid config to a temp YAML file and create source dir."""
    config_file = tmp_path / "test.yaml"
    config_file.write_text(yaml.dump(valid_config_dict))
    # Create the source directory so path validation passes
    source_dir = Path(valid_config_dict["directories"][0]["source"])
    source_dir.mkdir(parents=True, exist_ok=True)
    return str(config_file)


# ── Loading & Parsing ────────────────────────────────────────

class TestLoadConfig:
    def test_loads_valid_config(self, valid_config_file):
        config = load_config(valid_config_file)
        assert isinstance(config, CloudSyncConfig)
        assert config.project.name == "test-project"
        assert config.remote.bucket == "test-bucket"
        assert config.remote.existing is False
        assert config.remote.type == "s3"
        assert len(config.directories) == 1
        assert config.directories[0].name == "data"

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(empty))
        assert "YAML mapping" in str(exc_info.value)

    def test_non_dict_yaml(self, tmp_path):
        bad = tmp_path / "list.yaml"
        bad.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError):
            load_config(str(bad))

    def test_expands_home_dir(self, valid_config_dict, tmp_path):
        valid_config_dict["project"]["log_dir"] = "~/cloudsync-logs"
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml.dump(valid_config_dict))
        config = load_config(str(config_file), check_paths=False)
        assert "~" not in config.project.log_dir
        assert os.path.expanduser("~") in config.project.log_dir

    def test_defaults_applied(self, valid_config_file):
        config = load_config(valid_config_file)
        # Sync defaults
        assert config.sync.transfers == 32
        assert config.sync.checkers == 32
        assert config.sync.debounce_seconds == 30
        # Schedule defaults
        assert config.schedule.realtime is True
        assert config.schedule.weekly_full.day == "sunday"
        assert config.schedule.monthly_audit.day == 1

    def test_custom_sync_values(self, valid_config_dict, tmp_path):
        valid_config_dict["sync"] = {"transfers": 64, "checkers": 16, "debounce_seconds": 60}
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml.dump(valid_config_dict))
        config = load_config(str(config_file), check_paths=False)
        assert config.sync.transfers == 64
        assert config.sync.checkers == 16
        assert config.sync.debounce_seconds == 60


# ── Validation Errors ────────────────────────────────────────

class TestValidation:
    def test_missing_project(self, valid_config_dict, tmp_path):
        del valid_config_dict["project"]
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("project" in e for e in exc_info.value.errors)

    def test_missing_remote(self, valid_config_dict, tmp_path):
        del valid_config_dict["remote"]
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("remote" in e for e in exc_info.value.errors)

    def test_missing_directories(self, valid_config_dict, tmp_path):
        del valid_config_dict["directories"]
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("directories" in e for e in exc_info.value.errors)

    def test_empty_directories_list(self, valid_config_dict, tmp_path):
        valid_config_dict["directories"] = []
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("at least one" in e for e in exc_info.value.errors)

    def test_duplicate_directory_names(self, valid_config_dict, tmp_path):
        valid_config_dict["directories"].append({
            "name": "data",  # same name as first
            "source": "/tmp/other",
            "dest": "other",
        })
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("duplicate" in e for e in exc_info.value.errors)

    def test_invalid_remote_type(self, valid_config_dict, tmp_path):
        valid_config_dict["remote"]["type"] = "dropbox"
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("s3, gcs, azure, sftp" in e for e in exc_info.value.errors)

    def test_invalid_transfers(self, valid_config_dict, tmp_path):
        valid_config_dict["sync"] = {"transfers": -5}
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("positive integer" in e for e in exc_info.value.errors)

    def test_invalid_schedule_day(self, valid_config_dict, tmp_path):
        valid_config_dict["schedule"] = {"weekly_full": {"day": "notaday"}}
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("day name" in e for e in exc_info.value.errors)

    def test_source_path_not_exists(self, valid_config_dict, tmp_path):
        valid_config_dict["directories"][0]["source"] = "/nonexistent/path"
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=True)
        assert any("does not exist" in e for e in exc_info.value.errors)

    def test_skip_path_check(self, valid_config_dict, tmp_path):
        valid_config_dict["directories"][0]["source"] = "/nonexistent/path"
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(valid_config_dict))
        # Should NOT raise when check_paths=False
        config = load_config(str(f), check_paths=False)
        assert config.directories[0].source == "/nonexistent/path"

    def test_collects_all_errors(self, tmp_path):
        """Config with multiple errors should report ALL of them, not just the first."""
        bad_config = {
            "project": {"name": ""},  # empty name
            "remote": {
                "name": "x",
                "bucket": "b",
                "type": "invalid",
            },
            "directories": [],
        }
        f = tmp_path / "bad.yaml"
        f.write_text(yaml.dump(bad_config))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        # Should have multiple errors, not just the first one found
        assert len(exc_info.value.errors) >= 3


# ── Remote Mode A / B ─────────────────────────────────────────

class TestRemoteModes:
    def test_existing_remote_minimal_config(self, tmp_path):
        """Mode A: only name + bucket, no type/provider/region."""
        config_dict = {
            "project": {"name": "test", "log_dir": "/tmp/test"},
            "remote": {"name": "mys3", "bucket": "my-bucket"},
            "directories": [{"name": "d", "source": "/tmp/x", "dest": "d"}],
        }
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump(config_dict))
        config = load_config(str(f), check_paths=False)
        assert config.remote.existing is True
        assert config.remote.type is None
        assert config.remote.provider is None
        assert config.remote.region is None

    def test_new_remote_requires_provider(self, tmp_path):
        """Mode B: type provided but missing provider should fail."""
        config_dict = {
            "project": {"name": "test", "log_dir": "/tmp/test"},
            "remote": {"name": "mys3", "bucket": "b", "type": "s3"},
            "directories": [{"name": "d", "source": "/tmp/x", "dest": "d"}],
        }
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump(config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("provider" in e for e in exc_info.value.errors)

    def test_new_remote_requires_region(self, tmp_path):
        """Mode B: type + provider but missing region should fail."""
        config_dict = {
            "project": {"name": "test", "log_dir": "/tmp/test"},
            "remote": {
                "name": "mys3",
                "bucket": "b",
                "type": "s3",
                "provider": "AWS",
            },
            "directories": [{"name": "d", "source": "/tmp/x", "dest": "d"}],
        }
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump(config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("region" in e for e in exc_info.value.errors)

    def test_missing_remote_bucket(self, tmp_path):
        config_dict = {
            "project": {"name": "test", "log_dir": "/tmp/test"},
            "remote": {"name": "onlyname"},
            "directories": [{"name": "d", "source": "/tmp/x", "dest": "d"}],
        }
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump(config_dict))
        with pytest.raises(ConfigError) as exc_info:
            load_config(str(f), check_paths=False)
        assert any("remote.bucket" in e for e in exc_info.value.errors)

    def test_empty_type_string_treated_as_mode_a(self, tmp_path):
        """Blank `type:` should behave like omitted (Mode A)."""
        config_dict = {
            "project": {"name": "test", "log_dir": "/tmp/test"},
            "remote": {"name": "mys3", "bucket": "b", "type": "   "},
            "directories": [{"name": "d", "source": "/tmp/x", "dest": "d"}],
        }
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump(config_dict))
        config = load_config(str(f), check_paths=False)
        assert config.remote.existing is True
        assert config.remote.type is None


# ── Template Generation ──────────────────────────────────────

class TestTemplate:
    def test_generates_valid_yaml(self):
        template = generate_template()
        parsed = yaml.safe_load(template)
        assert isinstance(parsed, dict)
        assert "project" in parsed
        assert "remote" in parsed
        assert "directories" in parsed

    def test_template_passes_schema_validation(self, tmp_path):
        template = generate_template()
        f = tmp_path / "template.yaml"
        f.write_text(template)
        # Should pass schema validation (not path validation)
        config = load_config(str(f), check_paths=False)
        assert config.project.name == "my-sync-project"


# ── CLI Tests ────────────────────────────────────────────────

class TestCLI:
    def test_cli_validate_valid(self, valid_config_file):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", "--config", valid_config_file])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()
        assert "Mode B" in result.output

    def test_cli_validate_mode_a_minimal(self, tmp_path):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        cfg = tmp_path / "modea.yaml"
        cfg.write_text(
            yaml.dump({
                "project": {"name": "p", "log_dir": "/tmp"},
                "remote": {"name": "r", "bucket": "buck"},
                "directories": [{"name": "d", "source": "/tmp/z", "dest": "d"}],
            })
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", "--config", str(cfg), "--no-check-paths"])
        assert result.exit_code == 0
        assert "Mode A" in result.output

    def test_cli_validate_invalid(self, tmp_path):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.dump({"nothing": "here"}))
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", "--config", str(bad)])
        assert result.exit_code != 0

    def test_cli_init_creates_file(self, tmp_path):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        output = tmp_path / "new-config.yaml"
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--output", str(output)])
        assert result.exit_code == 0
        assert output.exists()

    def test_cli_init_no_overwrite(self, tmp_path):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        existing = tmp_path / "exists.yaml"
        existing.write_text("existing content")
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--output", str(existing)])
        assert result.exit_code != 0
        assert existing.read_text() == "existing content"  # Not overwritten

    def test_cli_init_force_overwrite(self, tmp_path):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        existing = tmp_path / "exists.yaml"
        existing.write_text("old content")
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--output", str(existing), "--force"])
        assert result.exit_code == 0
        assert existing.read_text() != "old content"

    def test_cli_version(self):
        from click.testing import CliRunner
        from cloudsync.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert "0.1.0" in result.output