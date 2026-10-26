"""End-to-end tests against real BEC 4 services, through the pytest_bec_e2e plugin.

They are skipped unless asked for:

    python -m pytest --start-servers tests/e2e         # start Redis and all BEC services
    BEC_MCP_E2E=1 python -m pytest --bec-redis-host localhost --redis-port 6379 tests/e2e

The second form uses services that are already running and only reads from them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    if config.getoption("--start-servers", default=False) or os.environ.get("BEC_MCP_E2E"):
        return
    skip = pytest.mark.skip(
        reason="needs BEC services: pass --start-servers, or set BEC_MCP_E2E=1 for running ones"
    )
    for item in items:
        if _HERE in Path(item.path).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_config_yaml_file_path():
    """Device config pytest_bec_e2e copies next to the services; these tests load none."""
    import bec_lib  # pylint: disable=import-outside-toplevel

    return os.path.join(os.path.dirname(bec_lib.__file__), "tests", "test_config.yaml")
