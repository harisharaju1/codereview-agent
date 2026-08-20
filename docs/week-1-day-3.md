# Day 3 — Fetch PRs and Diffs

## What was built

- **`src/schemas/pull_request.py`** — `PullRequestSummary` (`number`, `title`, `user` reusing `InstallationAccount`, `state`, `updated_at`) and `PullRequestSummaryList`, a `RootModel[list[PullRequestSummary]]` for GitHub's PR-list endpoint, which returns a bare JSON array rather than a wrapping object.
- **`src/services/pull_requests.py`** — `list_open_pull_requests` (`GET /repos/{owner}/{repo}/pulls?state=open&per_page=100`) and `fetch_pull_request_diff` (`GET /repos/{owner}/{repo}/pulls/{number}` with `Accept: application/vnd.github.v3.diff`, returning the raw diff text rather than a parsed model). Both take `client` and a plain `installation_token: str`, with no knowledge of where the token came from or `Settings` at all.
- **`src/routers/pull_requests.py`** — `GET /github-app/repos/{owner}/{repo}/pulls` and `GET /github-app/repos/{owner}/{repo}/pulls/{number}/diff`, both depending on `get_current_installation_id` and `installation_token_cache.get_installation_token`, the same chain `/installations/current` already established. A shared `_translate_github_error` helper turns a GitHub `404` into this service's own `404`, and anything else (rate limits, transient 5xx) into a `502`.
- **`tests/test_pull_requests_router.py`** — 6 tests: 401 without a cookie, a successful PR list, a successful diff fetch (asserting the raw text body and `text/plain` content type), and 404-translation for both endpoints.
- **`src/main.py`** — registered the new router alongside `github_app.router`.

## Why it's built this way

- **A new router, not more routes on `github_app.py`.** `github_app.py` is the install/auth lifecycle (install, callback, "what can I see"); PR/diff fetching is a different concern that happens to depend on that lifecycle's output. Splitting them means "how does auth work" and "how do we fetch PR data" stay independently findable.
- **The diff endpoint returns a plain `Response`, not a Pydantic model.** A diff is inherently unstructured text — forcing it through a schema would add ceremony without adding any validation value. The "typed boundaries" pattern from `architecture-patterns.md` is about validating shape where shape actually exists, not everywhere by default.
- **A `RootModel` for the PR list, instead of loosening validation to "just trust the list."** GitHub's PR-list response has no wrapping object the way the installation-repositories endpoint does, but the response is still worth validating item-by-item — `RootModel` is Pydantic's way of saying "the whole payload IS the list," not "the payload has a list field."
- **404 for both "doesn't exist" and "not visible to this installation," collapsed into one response, deliberately.** This mirrors GitHub's own API behavior — GitHub never distinguishes the two either, specifically so a caller without access can't use the difference between 404 and 403 to infer that a private repo exists. Preserving that ambiguity here rather than trying to be more precise than GitHub itself matches the actual security property being protected.
- **Rate-limit handling explicitly deferred, not half-built.** Anything that isn't a 404 becomes a flat `502` today. Building real retry/backoff around GitHub's rate-limit response is Day 4's dedicated task — folding a partial version in today would mean neither day does it properly.

## Python-specific things worth calling out

- **`RootModel[list[T]]`** is Pydantic's way of validating a response whose top level *is* a list, not an object with a list field — `PullRequestSummaryList.model_validate(response.json()).root` unwraps it back to a plain `list[PullRequestSummary]` once validated.
- **Returning `Response(content=..., media_type="text/plain")` directly** is how a FastAPI route opts out of its default "serialize my return value as JSON" behavior — necessary any time the body genuinely isn't JSON.
- **`httpx.HTTPStatusError`** (raised by `raise_for_status()`) carries the original `response` object as an attribute (`exc.response.status_code`), so translating an upstream failure into a different local exception doesn't require re-fetching or re-parsing anything — the failed response is already sitting right there on the caught exception.

## .NET parallel

- GitHub's `Accept`-header content negotiation (same URL, JSON metadata vs. raw diff text depending on the header sent) ≈ ASP.NET Core's own `Accept`-based content negotiation for an action supporting multiple `[Produces]` formats.
- Catching `httpx.HTTPStatusError` and mapping it to a different `HTTPException` ≈ catching a typed exception from an `HttpClient` call and translating it into a specific `IActionResult` (`NotFound()`, `StatusCode(502)`) rather than letting it bubble up as an unhandled 500.
- Returning a raw string body (`Response(content=diff_text, ...)`) instead of wrapping it in a DTO ≈ returning `ContentResult`/`string` from a controller action instead of a strongly-typed view model, when the content genuinely doesn't have a shape worth modeling.

## Verified manually

- `GET /github-app/repos/{owner}/{repo}/pulls` with no installation cookie → `401`.
- `GET /github-app/repos/{owner}/{repo}/pulls` against a real repo the installed app can see, using the browser-obtained installation cookie → returned real open PR data.
- `GET /github-app/repos/{owner}/{repo}/pulls/{number}/diff` against a real PR → returned real diff text.
- `uv run pytest` (17 tests) and `uv run ruff check .` both clean.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Typed, single-purpose schemas per external shape** (`PullRequestSummary`) continues the same discipline from Day 1/2 — resisting the temptation to reuse an unrelated existing model just because a couple of fields overlap.
- **A raw string return type for the diff, deliberately not wrapped in a schema,** is a new judgment call for this project: the "typed boundaries" pattern isn't "validate everything," it's "validate where a real shape exists" — this is the first place that distinction actually mattered in practice.
- **GitHub's 404-for-both-"not found"-and-"not visible"** is a genuinely new access-control nuance for this project — the earlier TypeScript project had no resource-visibility boundary of this kind, since it operated only on documents the caller already had.
- **Deferring rate-limit handling to its own dedicated day** rather than folding a partial version into today's work repeats the same "build backoff/retry correctly and deliberately, not half-done alongside something else" instinct as the equivalent day in the earlier project's Week 1.
