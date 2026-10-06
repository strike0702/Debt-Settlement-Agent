"""Shared pytest config: registers the ``live`` marker (real provider calls, opt-in)."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: real provider calls; needs DSA_LIVE=1 and keys (skipped otherwise)"
    )
