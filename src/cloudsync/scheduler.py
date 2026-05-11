"""Cron job generation and install/remove.

Phase 4 responsibilities:

- Read ``schedule`` from config: ``weekly_full``, ``monthly_audit``.
- Generate crontab lines that invoke ``cloudsync`` with the same ``--config``.
- Install/remove via ``crontab`` subprocess (document idempotency and backup of user crontab).

Weekly job may use ``rclone sync --max-age`` as a safety net; monthly may run full checksum audit.
"""

from __future__ import annotations
