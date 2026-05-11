"""Weekly diff reports and version / tree tracking.

Phase 4 responsibilities:

- After syncs, append structured records (path, timestamps, hash transitions if available).
- Generate weekly summaries; optional ISO week selector (e.g. ``--week 2026-W18``).
- Store tree snapshots (e.g. ``rclone lsjson``) as JSON for audit.

Align with ``sync.version_dir`` / backup layout from config.
"""

from __future__ import annotations
