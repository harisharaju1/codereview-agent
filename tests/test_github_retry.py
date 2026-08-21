import asyncio
import time

import httpx
import pytest

from src.services import github_retry
from src.services.github_retry import call_with_retry

TEST_URL = "https://api.github.com/test"


# Patching asyncio.sleep to a no-op (that just records how long it *would*
# have slept) is what keeps this whole test file fast — without it, the
# retry-then-succeed tests below would genuinely pause for real backoff
# delays every time the suite runs.
@pytest.fixture(autouse=True)
def _sleep_calls(monkeypatch):
    calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return calls


async def test_retries_transient_5xx_then_succeeds(respx_mock, _sleep_calls):
    route = respx_mock.get(TEST_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )

    async with httpx.AsyncClient() as client:
        response = await call_with_retry(lambda: client.get(TEST_URL))

    assert response.status_code == 200
    assert route.call_count == 2
    assert len(_sleep_calls) == 1


async def test_exhausts_retries_and_raises(respx_mock, _sleep_calls):
    route = respx_mock.get(TEST_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await call_with_retry(lambda: client.get(TEST_URL), max_attempts=3)

    assert exc_info.value.response.status_code == 503
    assert route.call_count == 3


async def test_rate_limit_waits_for_reset_then_succeeds(respx_mock, _sleep_calls):
    reset_at = int(time.time()) + 5
    route = respx_mock.get(TEST_URL).mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_at)},
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with httpx.AsyncClient() as client:
        response = await call_with_retry(lambda: client.get(TEST_URL))

    assert response.status_code == 200
    assert route.call_count == 2
    # The slept duration should track the reset time (~5s), not the
    # exponential-backoff formula used for plain 5xxs.
    assert 0 <= _sleep_calls[0] <= 5


async def test_404_is_never_retried(respx_mock, _sleep_calls):
    route = respx_mock.get(TEST_URL).mock(return_value=httpx.Response(404))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await call_with_retry(lambda: client.get(TEST_URL))

    assert exc_info.value.response.status_code == 404
    assert route.call_count == 1
    assert _sleep_calls == []


async def test_plain_403_without_rate_limit_headers_is_not_retried(respx_mock, _sleep_calls):
    # A 403 without the rate-limit-specific headers means "not allowed to do
    # this at all" — a genuinely different condition from rate limiting, and
    # not one that retrying could ever fix.
    route = respx_mock.get(TEST_URL).mock(return_value=httpx.Response(403))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_retry(lambda: client.get(TEST_URL))

    assert route.call_count == 1
    assert _sleep_calls == []


def test_backoff_delay_increases_with_attempt():
    first = github_retry._backoff_delay(1)
    second = github_retry._backoff_delay(2)

    # Jitter makes exact values non-deterministic, but the exponential
    # component should still push attempt 2's *minimum* possible delay
    # above attempt 1's *maximum* possible one.
    assert first < github_retry._BASE_DELAY_SECONDS * 2
    assert second >= github_retry._BASE_DELAY_SECONDS * 2


def test_seconds_until_reset_never_negative():
    past_reset = httpx.Response(403, headers={"X-RateLimit-Reset": str(int(time.time()) - 100)})

    assert github_retry._seconds_until_reset(past_reset) == 0
