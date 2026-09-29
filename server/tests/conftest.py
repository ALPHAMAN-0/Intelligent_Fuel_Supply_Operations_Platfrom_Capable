from __future__ import annotations

import os

import pytest

from tests.stack import make_stack


def pytest_collection_modifyitems(config, items):
    # Integration tests need the real simulator: SIM_BASE_URL=http://localhost:8000 pytest -m integration
    if os.environ.get("SIM_BASE_URL") is None:
        skip = pytest.mark.skip(reason="SIM_BASE_URL not set (real simulator not available)")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


@pytest.fixture
async def stack(tmp_path):
    s = await make_stack(tmp_path)
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def stack_factory(tmp_path):
    created = []

    async def factory(**overrides):
        s = await make_stack(tmp_path / f"s{len(created)}", **overrides)
        created.append(s)
        return s

    yield factory
    for s in created:
        await s.close()
