"""rclone CLI wrapper — copy, sync, check.

Phase 3 responsibilities:

- Build ``rclone`` argument lists from validated config (remote name, paths, transfers,
  ``--backup-dir`` / version dir when ``sync.backup_versions`` is true).
- Support dry-run for ``cloudsync diff``.
- Run ``copy`` / ``sync`` with ``--files-from`` when the hasher produces a file list.
- Optional retries and structured error reporting via ``logger``.

**Remote modes (``config.remote``):**

- If ``config.remote.existing`` is True (Mode A), the remote is already defined in rclone;
  do not run ``rclone config create`` from the syncer — only invoke sync/copy/check using
  ``remote.name``. Bucket in YAML is for display and path layout hints alongside ``dest``.
- If ``existing`` is False (Mode B), a future ``setup`` flow must create the remote before
  the syncer runs heavy operations; the syncer may assert the remote exists or call setup.

Phase 4+: integrate **lockfile** so cron and fswatch-triggered syncs do not overlap.
"""

from __future__ import annotations
