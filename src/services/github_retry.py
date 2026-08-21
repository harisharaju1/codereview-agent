import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import httpx

# Capped attempts, same reasoning in both branches below: a `5xx` might
# genuinely be transient, but "genuinely transient" doesn't mean "worth
# retrying forever" — an unbounded loop just turns a real, permanent outage
# into a hang instead of a clear failure.
_MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 0.5


# Summary: detects GitHub's specific "primary rate limit exhausted" shape.
# Exists to distinguish that case from an ordinary 403 (not allowed at all,
# never worth retrying) using GitHub's own rate-limit headers.
def _is_rate_limited(response: httpx.Response) -> bool:
    # GitHub signals its *primary* rate limit via 403 (not the more common
    # 429), plus this specific header pair — a plain 403 can also mean
    # "you're not allowed to do this at all," which is not a rate limit and
    # must not be retried, so both conditions are checked together.
    return response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"


# Summary: identifies a 5xx as a plausibly-transient server-side failure.
# Exists to separate "GitHub's infrastructure hiccuped, worth retrying"
# from client-side errors (4xx) that retrying could never fix.
def _is_transient_server_error(response: httpx.Response) -> bool:
    return 500 <= response.status_code < 600


# Summary: converts GitHub's X-RateLimit-Reset timestamp into a "how long
# to wait from now" duration. Exists so call_with_retry can sleep exactly
# as long as GitHub says is needed, instead of guessing with backoff.
def _seconds_until_reset(response: httpx.Response) -> float:
    reset_at = int(response.headers["X-RateLimit-Reset"])
    # GitHub tells us exactly when the limit resets — never negative, since
    # a reset time already in the past just means "retry immediately."
    return max(reset_at - time.time(), 0)


# Summary: computes an exponential-backoff wait with random jitter for a
# given attempt number. Exists specifically to avoid the thundering-herd
# problem — jitter stops many callers who failed simultaneously from all
# retrying at exactly the same instant.
def _backoff_delay(attempt: int) -> float:
    # Exponential growth (attempt 1 -> base, attempt 2 -> 2x base, ...) plus
    # a random jitter component. Jitter matters specifically when *multiple*
    # callers all failed at the same moment: without it, they'd all wake up
    # and retry at the exact same instant, recreating the overload that
    # caused the failure in the first place.
    return _BASE_DELAY_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, _BASE_DELAY_SECONDS)


# Summary: runs an HTTP call with GitHub-aware retry policy — honors
# X-RateLimit-Reset on rate limits, backs off with jitter on transient
# 5xxs, and never retries anything else. Exists as the single retry
# mechanism every GitHub-calling function in this project routes through.
async def call_with_retry(
    request_fn: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> httpx.Response:
    # `request_fn` is a zero-argument callable (typically a `lambda:
    # client.get(...)`) rather than this function taking a URL/method/etc.
    # itself — that keeps the retry policy generic across every kind of
    # GitHub call this project makes, instead of duplicating this loop once
    # per call site.
    for attempt in range(1, max_attempts + 1):
        response = await request_fn()
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            if _is_rate_limited(response):
                if attempt == max_attempts:
                    raise
                await asyncio.sleep(_seconds_until_reset(response))
                continue
            if _is_transient_server_error(response) and attempt < max_attempts:
                await asyncio.sleep(_backoff_delay(attempt))
                continue
            # Anything else (404, 401, 422, or a 5xx/rate-limit with no
            # attempts left) is not retried — `raise` with no argument
            # re-raises the exception already being handled, preserving its
            # original traceback rather than constructing a new one.
            raise
        return response

    # Unreachable: the loop above always either returns or raises before
    # running out of iterations. Present only so type checkers see every
    # path returning an httpx.Response.
    raise AssertionError("unreachable")
