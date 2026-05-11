"""Structured logging for all components.

Responsibilities (implement as you wire modules):

- Use ``project.log_dir`` from config; per-directory or per-component log files if useful.
- JSON or key=value structured lines for machine parsing.
- Size-based rotation or integration with system ``logrotate`` (see architecture gaps).

Import this from ``cli``, ``watcher``, ``syncer``, etc., once you add a concrete setup function.
"""

from __future__ import annotations
