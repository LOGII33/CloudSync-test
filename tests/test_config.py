"""Phase 1: config validation tests.

Target: loading ``configs/test.yaml`` (or a temp YAML) and asserting schema validation
errors for missing keys / bad types.

Example::

    def test_validate_good_config():
        ...

    def test_validate_rejects_missing_remote():
        ...
"""

from __future__ import annotations

import cloudsync


def test_package_importable() -> None:
    assert cloudsync.__version__
