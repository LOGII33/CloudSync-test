"""YAML config loader and validator.

Phase 1 responsibilities:

- Load the single YAML file that drives fswatch, rclone, and cron.
- Validate required keys: ``project``, ``remote``, ``directories``, ``sync``, ``schedule``.
- Resolve paths; optionally verify ``directories[].source`` exists when validating for run.
- Return a typed structure (dataclasses or pydantic model) for the rest of the package.

Config maps to tools (from architecture):

- **fswatch**: ``directories[].source``, ``directories[].exclude``, ``sync.debounce_seconds``
- **rclone**: ``remote.*``, ``directories[].dest``, ``sync.transfers``, ``sync.checksum``, etc.
- **cron**: ``schedule.weekly_full.*``, ``schedule.monthly_audit.*``

Do not read secrets from YAML; document env vars for rclone only.
"""

from __future__ import annotations
