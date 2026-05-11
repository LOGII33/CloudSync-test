"""rclone CLI wrapper — copy, sync, check.

Phase 3 responsibilities:

- Build ``rclone`` argument lists from validated config (remote name, paths, transfers,
  ``--backup-dir`` / version dir when ``sync.backup_versions`` is true).
- Support dry-run for ``cloudsync diff``.
- Run ``copy`` / ``sync`` with ``--files-from`` when the hasher produces a file list.
- Optional retries and structured error reporting via ``logger``.

Phase 4+: integrate **lockfile** so cron and fswatch-triggered syncs do not overlap.
"""

from __future__ import annotations
