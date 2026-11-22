import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import duckdb
import pytest
import pytest_asyncio

from hacore.bronze import ensure_schema
from hacore.lake import connect_local, install_extensions
from hacore.testing import FakeHA, running_fake_ha


@pytest_asyncio.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as running:
        yield running


@pytest.fixture
def lake(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """DuckLake with a local catalog and data path; bronze tables created."""
    ext_dir = os.environ.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None
    install_extensions(ext_dir)
    con = connect_local(
        str(tmp_path / "catalog.ducklake"), str(tmp_path / "data") + "/", extension_dir=ext_dir
    )
    ensure_schema(con)
    try:
        yield con
    finally:
        con.close()
