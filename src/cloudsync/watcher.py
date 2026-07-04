"""cloudsync.watcher — fswatch subprocess manager with OS-aware monitor selection."""
from __future__ import annotations
import json, os, platform, signal, subprocess, threading, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
from cloudsync.config import CloudSyncConfig, DirectoryConfig
from cloudsync.logger import get_logger, ensure_log_dirs
log = get_logger("watcher")

# ── OS Detection ─────────────────────────────────────────────

@dataclass
class SystemInfo:
    os_name: str; os_release: str; architecture: str; selected_monitor: str
    available_monitors: List[str]; inotify_max_watches: Optional[int] = None
    inotify_max_queued: Optional[int] = None; inotify_max_instances: Optional[int] = None
    is_fallback: bool = False; warnings: List[str] = field(default_factory=list)
    def to_dict(self): return {k: getattr(self, k) for k in self.__dataclass_fields__}

MONITOR_PRIORITY = {"Linux":["inotify_monitor","poll_monitor"],"Darwin":["fsevents_monitor","kqueue_monitor","poll_monitor"],"FreeBSD":["kqueue_monitor","poll_monitor"],"OpenBSD":["kqueue_monitor","poll_monitor"],"Windows":["windows_monitor","poll_monitor"]}
INOTIFY_RECOMMENDED = {"max_user_watches":2097152,"max_user_instances":256,"max_queued_events":65536}

def _get_available_monitors():
    try:
        r = subprocess.run(["fswatch","--list-monitors"],capture_output=True,text=True,timeout=5)
        if r.returncode == 0: return [m.strip() for m in r.stdout.strip().split("\n") if m.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired): pass
    return []

def _read_inotify_limit(name):
    p = Path(f"/proc/sys/fs/inotify/{name}")
    if p.exists():
        try: return int(p.read_text().strip())
        except: pass
    return None

def detect_monitor():
    on = platform.system(); ore = platform.release(); arch = platform.machine()
    avail = _get_available_monitors(); warnings = []
    pri = MONITOR_PRIORITY.get(on, ["poll_monitor"]); sel = "poll_monitor"; fb = True
    for m in pri:
        if m in avail: sel = m; fb = (m == "poll_monitor"); break
    if not avail: sel = "unknown"; fb = True; warnings.append("fswatch not installed or not in PATH")
    if fb and avail: warnings.append(f"Using poll_monitor (slow fallback). Expected '{pri[0]}' for {on}.")
    iw = iq = ii = None
    if on == "Linux":
        iw = _read_inotify_limit("max_user_watches"); iq = _read_inotify_limit("max_queued_events"); ii = _read_inotify_limit("max_user_instances")
        if iw and iw < INOTIFY_RECOMMENDED["max_user_watches"]: warnings.append(f"inotify max_user_watches={iw:,} (need {INOTIFY_RECOMMENDED['max_user_watches']:,})")
        if iq and iq < INOTIFY_RECOMMENDED["max_queued_events"]: warnings.append(f"inotify max_queued_events={iq:,} (need {INOTIFY_RECOMMENDED['max_queued_events']:,})")
    return SystemInfo(os_name=on,os_release=ore,architecture=arch,selected_monitor=sel,available_monitors=avail,inotify_max_watches=iw,inotify_max_queued=iq,inotify_max_instances=ii,is_fallback=fb,warnings=warnings)

# ── Data Structures ──────────────────────────────────────────

class WatcherMetadata:
    def __init__(self, dir_name, source_path):
        self.dir_name=dir_name; self.source_path=source_path; self.pid=None; self.started_at=None; self.stopped_at=None
        self.event_count=0; self.last_event_at=None; self.last_event_path=None; self.status="stopped"; self.monitor=None
    def to_dict(self): return {"dir_name":self.dir_name,"source_path":self.source_path,"pid":self.pid,"started_at":self.started_at,"stopped_at":self.stopped_at,"event_count":self.event_count,"last_event_at":self.last_event_at,"last_event_path":self.last_event_path,"status":self.status,"monitor":self.monitor}
    @classmethod
    def from_dict(cls, d):
        m=cls(d["dir_name"],d["source_path"]); m.pid=d.get("pid"); m.started_at=d.get("started_at"); m.stopped_at=d.get("stopped_at"); m.event_count=d.get("event_count",0); m.last_event_at=d.get("last_event_at"); m.last_event_path=d.get("last_event_path"); m.status=d.get("status","stopped"); m.monitor=d.get("monitor"); return m

class FswatchEvent:
    def __init__(self, timestamp, path, event_type, dir_name): self.timestamp=timestamp; self.path=path; self.event_type=event_type; self.dir_name=dir_name
    def to_log_line(self): return f"{self.timestamp} {self.event_type:<10} {self.path}"

# ── Path Helpers ─────────────────────────────────────────────

def _pid_file(c,n): return Path(c.project.log_dir)/"pids"/f"{n}.pid"
def _meta_file(c,n): return Path(c.project.log_dir)/"pids"/f"{n}.meta.json"
def _event_log(c,n,date=None):
    if date is None: date = datetime.now().strftime("%Y-%m-%d")
    return Path(c.project.log_dir)/"fswatch"/f"{n}-{date}.log"
def _save_metadata(c,m): _meta_file(c,m.dir_name).write_text(json.dumps(m.to_dict(),indent=2))
def _load_metadata(c,n):
    p=_meta_file(c,n)
    if p.exists():
        try: return WatcherMetadata.from_dict(json.loads(p.read_text()))
        except: return None
    return None
def _is_process_alive(pid):
    try: os.kill(pid,0); return True
    except: return False

# ── fswatch Command & Parsing ────────────────────────────────

def _build_fswatch_cmd(dc, monitor=None):
    cmd=["fswatch","--recursive","--event-flags","--timestamp","--event","Created","--event","Updated","--event","Removed","--event","Renamed","--event","MovedFrom","--event","MovedTo"]
    if monitor and monitor not in ("unknown","poll_monitor"): cmd.extend(["--monitor",monitor])
    for p in dc.exclude: cmd.extend(["--exclude",p])
    cmd.append(dc.source); return cmd

def _parse_fswatch_line(line, dn, sp):
    line=line.strip()
    if not line: return None
    parts=line.split()
    if len(parts)<2: return None
    known={"Created","Updated","Removed","Renamed","MovedFrom","MovedTo","IsFile","IsDir","IsSymLink","Link","AttributeModified","OwnerModified"}
    pp,ep=[],[]
    for p in parts:
        if p in known: ep.append(p)
        elif ep: ep.append(p)
        else: pp.append(p)
    if not pp: return None
    fp=" ".join(pp); rp=fp.replace(sp,"").lstrip("/")
    et="Modified"
    for e in ep:
        if e in {"Created","Updated","Removed","Renamed","MovedFrom","MovedTo"}: et=e; break
    return FswatchEvent(datetime.now().isoformat(),rp or fp,et,dn)

def _log_writer_thread(proc,cfg,dc,meta):
    wl=get_logger(f"watcher.{dc.name}")
    try:
        for raw in iter(proc.stdout.readline,""):
            if not raw: break
            ev=_parse_fswatch_line(raw,dc.name,dc.source)
            if not ev: continue
            meta.event_count+=1; meta.last_event_at=ev.timestamp; meta.last_event_path=ev.path; _save_metadata(cfg,meta)
            with open(_event_log(cfg,dc.name),"a",encoding="utf-8") as f: f.write(ev.to_log_line()+"\n")
    except Exception as e: wl.error(f"Error: {e}")
    finally: meta.status="stopped"; meta.stopped_at=datetime.now().isoformat(); _save_metadata(cfg,meta)

# ── Public API: Watch Management ─────────────────────────────

def get_watched_directories(c): return [d for d in c.directories if d.watch]

def start_watcher(c,dc,system_info=None):
    dn=dc.name; ex=_load_metadata(c,dn)
    if ex and ex.pid and _is_process_alive(ex.pid): raise RuntimeError(f"Already running (PID {ex.pid})")
    if not Path(dc.source).exists(): raise FileNotFoundError(f"Not found: {dc.source}")
    ensure_log_dirs(c)
    if system_info is None: system_info=detect_monitor()
    cmd=_build_fswatch_cmd(dc,monitor=system_info.selected_monitor)
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    meta=WatcherMetadata(dn,dc.source); meta.pid=proc.pid; meta.started_at=datetime.now().isoformat(); meta.status="running"; meta.monitor=system_info.selected_monitor
    _pid_file(c,dn).write_text(str(proc.pid)); _save_metadata(c,meta)
    threading.Thread(target=_log_writer_thread,args=(proc,c,dc,meta),daemon=True).start()
    return meta

def stop_watcher(c,dn):
    pf=_pid_file(c,dn)
    if not pf.exists(): return False
    pid=int(pf.read_text().strip())
    if not _is_process_alive(pid): pf.unlink(missing_ok=True); return False
    try:
        os.kill(pid,signal.SIGTERM)
        for _ in range(50):
            if not _is_process_alive(pid): break
            time.sleep(0.1)
        else: os.kill(pid,signal.SIGKILL)
    except ProcessLookupError: pass
    pf.unlink(missing_ok=True); m=_load_metadata(c,dn)
    if m: m.status="stopped"; m.stopped_at=datetime.now().isoformat(); m.pid=None; _save_metadata(c,m)
    return True

def start_all(c):
    si=detect_monitor(); r={}
    for dc in get_watched_directories(c):
        try: r[dc.name]=start_watcher(c,dc,system_info=si)
        except Exception as e: m=WatcherMetadata(dc.name,dc.source); m.status="error"; r[dc.name]=m
    return r

def stop_all(c): return {dc.name:stop_watcher(c,dc.name) for dc in get_watched_directories(c)}

def get_status(c):
    s={}
    for dc in get_watched_directories(c):
        m=_load_metadata(c,dc.name)
        if m:
            if m.pid and not _is_process_alive(m.pid): m.status="dead"; m.stopped_at=m.stopped_at or datetime.now().isoformat(); _save_metadata(c,m)
            s[dc.name]=m
        else: s[dc.name]=WatcherMetadata(dc.name,dc.source)
    return s

# ── Public API: Log Reading ──────────────────────────────────

def read_events(c, dn, tail=50, date=None):
    if date is None: date = datetime.now().strftime("%Y-%m-%d")
    lf = Path(c.project.log_dir)/"fswatch"/f"{dn}-{date}.log"
    if not lf.exists(): return []
    lines = [l for l in lf.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
    return lines[-tail:] if tail and tail < len(lines) else lines

def get_changed_files(c, dn, date=None):
    """Get deduplicated changed files from a single day's fswatch log."""
    paths = set()
    for line in read_events(c, dn, tail=None, date=date):
        parts = line.split(maxsplit=2)
        if len(parts) >= 3: paths.add(parts[2])
        elif len(parts) == 2: paths.add(parts[1])
    return sorted(paths)

def get_changed_files_range(c, dn, days=8):
    """
    Get deduplicated changed files from multiple days of fswatch logs.
    Reads logs from today back to `days` days ago.
    Used by weekly smart sync to collect the full week's changes.
    """
    all_paths: Set[str] = set()
    for days_ago in range(days):
        date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        day_files = get_changed_files(c, dn, date=date)
        all_paths.update(day_files)
    return sorted(all_paths)

def find_recent_files(source_path: str, max_age_days: int = 8) -> List[str]:
    """
    Scan a directory for files modified within max_age_days.
    Uses `find -mtime` — fast filesystem scan, no network.
    Returns relative paths.
    """
    try:
        result = subprocess.run(
            ["find", source_path, "-type", "f", "-mtime", f"-{max_age_days}"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return []
        prefix = source_path.rstrip("/") + "/"
        paths = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                rel = line.replace(prefix, "", 1) if line.startswith(prefix) else line
                paths.append(rel)
        return sorted(paths)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
