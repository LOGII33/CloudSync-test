"""fswatch subprocess manager and log parsing.

Phase 2 responsibilities:

- Start one ``fswatch`` process per directory where ``watch: true``.
- Parse fswatch stdout/stderr or per-directory log files into normalized paths.
- Implement PID file management for ``watch start`` / ``watch stop``.
- Apply ``directories[].exclude`` and debounce using ``sync.debounce_seconds`` before
  notifying the sync pipeline (Phase 3).

Tips from architecture: Linux inotify watch limits, RAM use on huge trees, log rotation.
"""

from __future__ import annotations
