"""Contract tests against a real Home Assistant in a container.

Enabled by ``HAMMOND_CONTRACT_HA_URL`` (a fresh, not yet onboarded instance started with
the configuration in ``ha-config/``). ``HAMMOND_CONTRACT_HA_CONFIG`` optionally points
to that configuration directory on the host, for the recorder import test. See the
``contract`` job in .github/workflows/ci.yml for how CI runs them.
"""

import json
import os
from pathlib import Path

import pytest

from hacore.onboarding import OnboardedTokens, onboard

HA_URL = os.environ.get("HAMMOND_CONTRACT_HA_URL", "").rstrip("/")
# Onboarding works only once per instance; keep tokens for repeated local runs.
TOKEN_CACHE = os.environ.get("HAMMOND_CONTRACT_TOKEN_CACHE")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "contract" in item.nodeid:
            item.add_marker(pytest.mark.contract)
            if not HA_URL:
                item.add_marker(pytest.mark.skip(reason="HAMMOND_CONTRACT_HA_URL not set"))


@pytest.fixture(scope="session")
def ha_url() -> str:
    return HA_URL


@pytest.fixture(scope="session")
def tokens() -> OnboardedTokens:
    if TOKEN_CACHE and Path(TOKEN_CACHE).is_file():
        return OnboardedTokens(**json.loads(Path(TOKEN_CACHE).read_text()))
    result = onboard(HA_URL)
    if TOKEN_CACHE:
        Path(TOKEN_CACHE).write_text(
            json.dumps({"admin_token": result.admin_token, "user_token": result.user_token})
        )
    return result
