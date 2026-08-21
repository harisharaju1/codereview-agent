# Day 4 Plan — Retry/Backoff Around GitHub Calls

## What we're doing and why

Every GitHub call this project makes so far (Day 2's installation/token fetches, Day 3's PR list/diff fetches) either succeeds or fails outright — a transient network blip or a rate limit produces the same unhandled `httpx.HTTPStatusError` (or, worse for the caller, an already-translated `502`) as a genuine, permanent failure. Day 4 adds real retry-with-backoff, hand-written rather than reached for as a library, specifically so the reasoning behind it (why jitter matters, why GitHub's rate-limit response deserves different handling than a plain 5xx) is actually understood rather than delegated.

This intentionally does **not** touch Day 3's `_translate_github_error` 404 handling — a `404` is not retried under any circumstances (retrying "this doesn't exist" just wastes calls and time), which is exactly why 404s were kept separate on Day 3 rather than folded into a generic error path.

---

## What GitHub actually tells you about failures worth retrying

- **Rate limiting**: GitHub returns `403` (not `429`) when the primary rate limit is exhausted, with an `X-RateLimit-Remaining: 0` header and an `X-RateLimit-Reset` header — a Unix timestamp telling you exactly when the limit resets. The correct behavior is to sleep until that timestamp, not guess with exponential backoff; GitHub is being explicit about when it'll accept requests again, and ignoring that in favor of blind backoff is worse, not more resilient.
- **Transient server errors**: a plain `5xx` (GitHub's own infrastructure hiccupping) has no such explicit signal — this is where exponential-backoff-with-jitter actually applies. Jitter (a small random offset added to each wait) exists specifically to stop many callers who all failed at the same moment from all retrying at the same moment again, which would just recreate the overload that caused the failure in the first place.
- **Everything else** (a `404`, a `401` from a bad/expired token, a `422`) is not a transient condition — retrying it wastes calls and can't succeed, so it should fail immediately, not get folded into the same retry loop.

---

## File 1 — `src/services/github_retry.py` — the retry mechanics

- `async def call_with_retry(request_fn, *, max_attempts: int = 3) -> httpx.Response` — takes a zero-argument async callable (typically a `lambda: client.get(...)` or `lambda: client.post(...)`) so the retry loop doesn't need to know anything about what kind of call it's wrapping. On each attempt:
  1. Call `request_fn()`, call `.raise_for_status()` on the result
  2. On success, return the response
  3. On `httpx.HTTPStatusError`, inspect the status:
     - `403` with `X-RateLimit-Remaining: 0` → compute `sleep_seconds = X-RateLimit-Reset - now`, `asyncio.sleep(sleep_seconds)`, retry (this doesn't count against `max_attempts` the same way — GitHub told us exactly when it'll work, so it isn't really "guessing and hoping")
     - `5xx` → exponential backoff with jitter (`base_delay * 2**attempt + random_jitter`), retry up to `max_attempts`, then re-raise
     - Anything else (`4xx` besides the rate-limit case) → re-raise immediately, no retry

**Why a generic wrapper function, not decorating each call site by hand:** every GitHub call in this project (Day 2's token/installation fetches, Day 3's PR/diff fetches) needs the exact same policy — writing the backoff math once and passing in *what* to call, rather than duplicating retry logic per function, is the same "isolate a cross-cutting concern" reasoning already used for `get_http_client` and `installation_token_cache`.

**Why this doesn't live inside `installation_token_cache.py` or `pull_requests.py` directly:** retry-with-backoff is orthogonal to what each of those modules actually does (caching a token; parsing a PR response) — it's infrastructure, the same category as the HTTP client itself, not business logic specific to any one of them.

---

## File 2 — wiring it into existing service calls

- `src/services/pull_requests.py`: both `list_open_pull_requests` and `fetch_pull_request_diff` wrap their `client.get(...)` call in `call_with_retry(...)` instead of calling `raise_for_status()` directly.
- `src/services/github_app_auth.py`: `fetch_installation_token` and `fetch_installation` do the same for their calls.

**Why not `installation_token_cache.py` itself:** the cache module calls into `github_app_auth.py`'s functions, which will already retry internally — wrapping the same call twice would just double the retry budget without adding anything.

---

## File 3 — `src/routers/pull_requests.py` — what changes here

Almost nothing. `_translate_github_error` still exists and still only handles the terminal case (an `httpx.HTTPStatusError` that retrying already gave up on) — it doesn't need to know retries happened before it saw the exception. This is a deliberate check on the design: if retry logic *did* require router-level changes, that would suggest it leaked out of the service layer where it belongs.

---

## .NET parallels

- Hand-written exponential backoff with jitter ≈ the same shape as `Polly`'s `WaitAndRetryAsync` with a jitter strategy — the point of writing it by hand once is understanding what a library like Polly is actually doing under the hood, not that Polly itself is a bad choice for production code.
- Respecting a server-provided `Retry-After`-style header (`X-RateLimit-Reset` here) over blind backoff ≈ the same "listen when the server tells you the exact wait time" instinct behind honoring an HTTP `Retry-After` header in a `.NET` `DelegatingHandler`.
- A retry wrapper taking a callable (`request_fn`) rather than being tied to one HTTP verb or call site ≈ a generic `Func<Task<T>>`-accepting retry helper in C#, or a `DelegatingHandler` inserted into an `HttpClient` pipeline so retry applies transparently to every call through that client.

---

## Automated verification (no real GitHub credentials needed)

All via `respx`, using its ability to return a *sequence* of responses for the same route (first N calls fail, the next succeeds) to simulate retry-then-success without a real network:

- A `5xx` followed by a `200` → the wrapped call ultimately succeeds, and the mocked route was hit more than once
- `max_attempts` consecutive `5xx` responses → the original `httpx.HTTPStatusError` is raised after all attempts are exhausted, not silently swallowed
- A `403` with `X-RateLimit-Remaining: 0` and an `X-RateLimit-Reset` a few seconds in the future → the call sleeps roughly that long (asserted via a monkeypatched `asyncio.sleep` rather than actually waiting in the test suite) and then succeeds
- A plain `404` → no retry attempted at all (the mocked route's `call_count` stays at `1`), and the router's existing 404-translation behavior from Day 3 is unaffected

## Manual verification

Rate limits and transient 5xxs aren't practical to trigger against the real GitHub API on demand, so manual verification here is limited to confirming nothing regressed:

```bash
uv run fastapi dev src/main.py
curl -b cookies.txt http://localhost:8000/github-app/repos/<owner>/<repo>/pulls
curl -b cookies.txt http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<number>/diff
# Both should behave exactly as they did at the end of Day 3 — retry logic
# should be invisible on the successful path.
```

---

## End-of-day checklist

- [ ] A transient `5xx` followed by success is retried and returns the successful result
- [ ] Exhausting all retry attempts on repeated `5xx`s still raises, rather than hanging or returning a bad result
- [ ] A `403` rate-limit response is handled via `X-RateLimit-Reset`, not blind exponential backoff
- [ ] A `404` is never retried
- [ ] `uv run pytest` and `uv run ruff check .` both pass
- [ ] A short write-up of any real asyncio-vs-Node-event-loop friction hit while building this (per the original Week 1 plan's Day 4 scope)

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Hand-written exponential backoff with jitter, capped attempts, tested via mocked failure sequences** is a direct repeat of a pattern built in the earlier TypeScript project's Week 1 — same reasoning (understand the backoff math before reaching for a library), applied again against a different upstream (GitHub's REST API instead of an LLM API).
- **New this time, and genuinely different from the earlier project**: GitHub's `X-RateLimit-Reset` header gives an exact wait time, which the earlier project's upstream (an LLM API) either didn't expose or wasn't built to honor the same way — "respect the server's stated retry time over guessing" is a refinement on the earlier backoff logic, not just a repeat of it.
- **A retry wrapper as its own small module, injected around calls rather than baked into them,** continues the same "cross-cutting concern gets isolated" instinct as `get_http_client` and `installation_token_cache.py` — by this point in the project, that's less a one-off decision and more a settled house style.
