from collections.abc import AsyncIterator

import pytest_asyncio

from hacore.testing import FakeHA, running_fake_ha


@pytest_asyncio.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as running:
        yield running
