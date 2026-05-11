"""Checksum comparison via rclone (e.g. ``check --files-from``).

Phase 3 responsibilities:

- Given a list of paths from the watcher / change detector, run checksum comparison
  against the remote only for those files (``rclone check --checksum --files-from``).
- Output the subset that actually differs (skip touched-but-unchanged).
- Feed that list to ``syncer`` for upload.

This module bridges **watcher output → needs-sync list → syncer**.
"""

from __future__ import annotations
