# cloudsync

Config-driven file sync: one YAML drives **fswatch** (real-time), **rclone** (copy/sync/check), and **cron** (weekly/monthly). This repo is scaffolded for incremental implementation and tests.

## Layout

- `src/cloudsync/` — Python package (see module docstrings for phase instructions).
- `configs/` — example and production-oriented YAML templates.
- `tests/` — add tests as you implement each phase.

## Install (development)

```bash
cd /path/to/Cloudsync
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Phased roadmap

| Phase | Focus | Target test |
|-------|--------|-------------|
| 1 | `config.py` (YAML load + validate), `cli.py` (Click skeleton) | `cloudsync validate --config configs/test.yaml` |
| 2 | `watcher.py` (fswatch subprocess, log parsing, PID files) | `cloudsync watch start/stop` on a small directory |
| 3 | `syncer.py`, `hasher.py`, pipeline from watcher → checksum → rclone | Edit file → detect → sync (dry-run first) |
| 4 | `scheduler.py`, `changelog.py`, lockfile for concurrent syncs | Cron install/remove, weekly reports |
| 5 | Production tuning | Real paths, logs, debounce, inotify limits |

## CLI commands (intended)

See `src/cloudsync/cli.py` docstring for the full command list to implement.

## External tools

- `rclone` on `PATH`
- `fswatch` (e.g. `sudo apt install fswatch` on Linux with inotify)
- `cron` / `crontab` for scheduled jobs

Do not embed secrets in YAML; use environment variables or encrypted rclone config.
