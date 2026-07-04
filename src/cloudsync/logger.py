"""cloudsync.logger — Structured logging."""
from __future__ import annotations
import json, logging, os
from datetime import datetime
from pathlib import Path
from cloudsync.config import CloudSyncConfig

class StructuredFormatter(logging.Formatter):
    def format(self, record):
        entry = {"timestamp": datetime.now().astimezone().isoformat(), "level": record.levelname, "component": record.name, "message": record.getMessage()}
        for k in ("directory","file_path","event_type","pid","file_count","duration_seconds","error"):
            v = getattr(record, k, None)
            if v is not None: entry[k] = v
        return json.dumps(entry)

class HumanFormatter(logging.Formatter):
    COLORS = {"DEBUG":"\033[90m","INFO":"\033[32m","WARNING":"\033[33m","ERROR":"\033[31m"}
    def format(self, record):
        c = self.COLORS.get(record.levelname, "")
        ts = datetime.now().strftime("%H:%M:%S")
        comp = record.name.replace("cloudsync.", "")
        d = getattr(record, "directory", ""); ds = f"[{d}] " if d else ""
        return f"{c}{ts} {record.levelname:<7}\033[0m {comp:>10} | {ds}{record.getMessage()}"

def ensure_log_dirs(config):
    base = Path(config.project.log_dir)
    dirs = {"base": base, "fswatch": base/"fswatch", "sync": base/"sync", "changelogs": base/"changelogs", "pids": base/"pids"}
    for d in dirs.values(): d.mkdir(parents=True, exist_ok=True)
    return {k: str(v) for k, v in dirs.items()}

def setup_logging(config, console=True, level="INFO"):
    log_dirs = ensure_log_dirs(config)
    root = logging.getLogger("cloudsync"); root.setLevel(getattr(logging, level.upper())); root.handlers.clear()
    fh = logging.FileHandler(str(Path(log_dirs["base"])/"cloudsync.log"), encoding="utf-8"); fh.setFormatter(StructuredFormatter()); root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(); ch.setFormatter(HumanFormatter()); ch.setLevel(getattr(logging, level.upper())); root.addHandler(ch)
    return log_dirs

def get_logger(component): return logging.getLogger(f"cloudsync.{component}")
