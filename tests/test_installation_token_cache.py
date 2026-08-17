from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.config.settings import get_settings
from src.schemas.github_app import InstallationAccessToken
from src.services import installation_token_cache

ACCESS_TOKEN_URL = "https://api.github.com/app/installations/123/access_tokens"


# The cache is module-level state (same shape as session_store.py's
# _sessions dict was) — without clearing it between tests, an earlier
# test's cached token would leak into a later one and hide real bugs.
@pytest.fixture(autouse=True)
def _clear_token_cache():
    installation_token_cache._tokens.clear()
    yield
    installation_token_cache._tokens.clear()


async def test_get_installation_token_fetches_and_caches(respx_mock):
    settings = get_settings()
    route = respx_mock.post(ACCESS_TOKEN_URL).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_fresh_token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    )

    async with httpx.AsyncClient() as client:
        first = await installation_token_cache.get_installation_token(client, settings, 123)
        second = await installation_token_cache.get_installation_token(client, settings, 123)

    assert first == "ghs_fresh_token"
    assert second == "ghs_fresh_token"
    # The second call should be served from the cache — only one real HTTP
    # request should ever have been made.
    assert route.call_count == 1


async def test_get_installation_token_refetches_when_stale(respx_mock):
    settings = get_settings()
    route = respx_mock.post(ACCESS_TOKEN_URL).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_new_token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    )
    # Seed the cache directly with a token that's already past its expiry
    # (and therefore past the refresh margin too) — simulating "an hour has
    # passed" without actually waiting an hour in the test.
    installation_token_cache._tokens[123] = InstallationAccessToken(
        token="ghs_stale_token",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    async with httpx.AsyncClient() as client:
        token = await installation_token_cache.get_installation_token(client, settings, 123)

    assert token == "ghs_new_token"
    assert route.call_count == 1
