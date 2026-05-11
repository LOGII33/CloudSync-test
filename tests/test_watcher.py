"""Phase 2: fswatch integration tests.

Target: ``watch start`` / ``watch stop`` against a small directory (e.g. 100 files),
PID file presence/absence, and log line parsing into paths.

Requires ``fswatch`` on PATH in CI or mark tests as integration-only.
"""

from __future__ import annotations
