"""Shared test configuration.

Integration tests run against real PostgreSQL + PostGIS and NATS. By default they start throwaway
containers with testcontainers; set ``TEST_DATABASE_URL`` (``postgresql://user:pass@host:port/db``)
and ``TEST_NATS_URL`` to reuse running services instead (CI does this to share one set of
containers across jobs).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile("default", deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("ci", parent=settings.get_profile("default"), max_examples=300)
settings.load_profile("default")

ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path = Path(str(item.fspath))
        if ROOT / "integration" in path.parents:
            item.add_marker(pytest.mark.integration)
        elif ROOT / "e2e" in path.parents:
            item.add_marker(pytest.mark.e2e)
