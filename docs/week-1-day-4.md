# Day 4 — Retry/Backoff Around GitHub Calls

## What was built

- **`src/services/github_retry.py`** — `call_with_retry(request_fn, max_attempts=3)`, a generic retry wrapper around a zero-argument async callable that returns an `httpx.Response`. On failure it distinguishes three cases: a rate-limit `403` (sleeps exactly until `X-RateLimit-Reset`), a transient `5xx` (exponential backoff with jitter, capped at `max_attempts`), or anything else (`404`, a bare `403`, `401`, `422`) — raised immediately, never retried.
- **`src/services/pull_requests.py`** and **`src/services/github_app_auth.py`** — all four GitHub-calling functions (`list_open_pull_requests`, `fetch_pull_request_diff`, `fetch_installation_token`, `fetch_installation`) now route their `client.get`/`client.post` call through `call_with_retry` instead of calling `raise_for_status()` directly.
- **`tests/test_github_retry.py`** — 8 tests: retry-then-succeed on a `5xx`, retries exhausted still raises, rate-limit path sleeps and succeeds, `404` never retried, a plain `403` (no rate-limit headers) never retried, plus direct unit tests on the backoff-delay and seconds-until-reset helpers.

## Why it's built this way

- **A single generic wrapper, not per-call-site retry logic.** Every GitHub call in this project needs the exact same policy — writing the backoff/rate-limit math once, and passing in *what* to call via a zero-argument callable, is the same "isolate a cross-cutting concern" instinct already used for `get_http_client` and `installation_token_cache`.
- **Rate limiting and transient errors are handled differently, deliberately.** GitHub's `403` + `X-RateLimit-Reset` is an explicit, exact instruction ("retry works again at this timestamp") — honoring it directly is strictly better than guessing with exponential backoff. A plain `5xx` carries no such signal, which is exactly where jittered exponential backoff belongs instead.
- **`404` (and any other non-retryable `4xx`) is never retried, on purpose.** Day 3's `_translate_github_error` already treats a `404` as an expected, terminal outcome (repo doesn't exist, or this installation can't see it) — retrying something that can't succeed just wastes calls and time. This is also why `github_retry.py` doesn't know or care about Day 3's error-translation logic at all: retry policy and error-shape translation are different concerns, and the router-level code didn't need to change to pick up retry behavior underneath it.
- **Rate-limit retries aren't unconditionally exempt from `max_attempts`.** An earlier version of the design let rate-limit waits retry forever, on the reasoning that GitHub told us exactly when it'd work again — but that assumes reset always arrives as promised, which isn't a safe assumption to build an unbounded loop on. Counting a rate-limit retry against the same attempt budget as a `5xx` keeps the failure mode "eventually raises" rather than "can hang indefinitely."

## Python-specific things worth calling out

- **`asyncio.sleep` is a bare module-level function, not an injectable dependency.** Testing "does this code wait the right amount of time" without actually waiting meant monkeypatching `asyncio.sleep` itself (`monkeypatch.setattr(asyncio, "sleep", fake_sleep)`), not swapping out some DI-provided clock abstraction — there isn't one in the standard library. This is a real difference from, say, `IHostedService`/`TimeProvider` patterns in newer .NET, which are built specifically to be mockable; Python's asyncio primitives assume you'll monkeypatch the module directly when you need to.
- **Lambdas as a way to pass "a call to make later" around** (`lambda: client.get(...)`) work here specifically because none of these lambdas are created inside a loop — Python closures capture variables by reference, not by value, which is the classic late-binding gotcha when a lambda is built inside a `for` loop and all of them end up seeing the loop's *final* value. Since `call_with_retry` is invoked once per request with a single lambda, that trap doesn't apply here, but it's the kind of thing worth remembering before reusing this pattern somewhere that does loop.
- **`respx`'s `side_effect=[...]` list** for mocking a sequence of different responses to the same route (fail, then succeed) is what made testing "retries transparently, then succeeds" possible without touching the real network — each call to the mocked route consumes the next item in the list.

## .NET parallel

- Hand-written exponential backoff with jitter ≈ `Polly`'s `WaitAndRetryAsync` with a jitter strategy — the point of writing it by hand once is understanding what a library like Polly does under the hood, not that Polly is a bad choice for production code.
- Honoring `X-RateLimit-Reset` over blind backoff ≈ respecting an HTTP `Retry-After` header in a `DelegatingHandler` — both are "the server told you the exact wait, use it" instincts.
- `call_with_retry` taking a `Callable[[], Awaitable[httpx.Response]]` ≈ a generic `Func<Task<T>>`-accepting retry helper, or a `DelegatingHandler` inserted into an `HttpClient` pipeline so retry applies transparently to every call made through that client.

## Verified manually

- Full existing manual flow re-run end-to-end (health check, unauthenticated `401`, PR list, PR diff) — behavior on the successful path is unchanged; retry logic is invisible when nothing fails.
- Rate limits and transient `5xx`s aren't practically triggerable against the real GitHub API on demand — this is covered by the automated `respx`-mocked tests instead, which exercise both paths directly with a fake, monkeypatched `asyncio.sleep`.
- `uv run pytest` (24 tests) and `uv run ruff check .` both clean.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Hand-written exponential backoff with jitter, capped attempts, tested via mocked failure sequences** is a direct repeat of a pattern built in the earlier TypeScript project's Week 1 — same reasoning, applied again against a different upstream (GitHub's REST API instead of an LLM API).
- **New this time**: GitHub's `X-RateLimit-Reset` gives an exact wait time, something the earlier project's upstream either didn't expose or wasn't built to honor the same way — "respect the server's stated retry time over guessing" is a refinement on the earlier backoff logic, not just a repeat of it.
- **The asyncio.sleep-is-a-bare-function-not-a-DI-seam friction** is a genuinely new observation for this project — the earlier TypeScript project's equivalent (`setTimeout`-based delay) has the same "not natively mockable" character, so this wasn't actually a Python-specific surprise so much as a reminder that both ecosystems require reaching for the same trick (monkeypatching/mocking the global timer function directly) rather than a clean injected abstraction.
- **A retry wrapper as its own small module, injected around calls rather than baked into them,** continues the same "cross-cutting concern gets isolated" instinct as `get_http_client` and `installation_token_cache.py` — by Day 4, this is a settled house style for this project rather than a fresh decision each time.
