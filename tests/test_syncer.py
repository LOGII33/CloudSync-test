"""Phase 3: rclone wrapper tests.

Use subprocess mocks or a test remote; assert argv built from config matches expectations.
Cover ``--files-from``, dry-run, and error exit codes without requiring a real 1.6TB sync.
"""

from __future__ import annotations
