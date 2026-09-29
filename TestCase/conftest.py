"""Conftest for the cross-platform pytest suite defined in `TestCase/`.

Each test file in this folder is a *specification*. The pytest files in this same
folder (the `test_*.py` ones) provide a runnable implementation of those
specifications that can be executed against the codebase without needing a
running simulator (they use the in-memory factories and `respx` for HTTP
mocking).

Pytest configuration:
    asyncio_mode = "auto"     (each test is auto-detected as async)
    markers:
        unit          - default; fast, no I/O
        integration   - requires a running simulator (deselected by default)

Run with:
    pytest -m unit          # only fast tests
    pytest                  # everything (unit + integration)
    pytest -m integration   # only integration tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project importable without installing it.
SERVER = Path(__file__).resolve().parent.parent / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Default-deselect integration tests unless explicitly requested."""
    if config.getoption("-m") in (None, ""):
        # No marker filter passed: drop integration tests by default.
        skip_integration = pytest.mark.skip(reason="integration tests need a running simulator; pass -m integration")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("asyncio_mode", help="pytest-asyncio mode", default="auto")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: fast unit tests, no I/O")
    config.addinivalue_line("markers", "integration: requires a running simulator")
    config.addinivalue_line("markers", "e2e: full end-to-end scenario")
