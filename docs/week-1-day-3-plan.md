# Day 3 Plan — Fetch PRs and Diffs

## What we're doing and why

Day 2 (in its final, GitHub App form) proved this service can authenticate as an installation and see which repos it can access (`GET /github-app/installations/current`). Day 3 does the thing the whole project actually exists for: given a repo this installation can see, list its open PRs, and fetch a given PR's actual diff text. Nothing does anything with that diff yet — the review logic starts Week 2. Today is purely "fetch the raw material correctly, with proper error handling."

Two endpoints, in a new `prs` router:
- `GET /github-app/repos/{owner}/{repo}/pulls` — list open PRs
- `GET /github-app/repos/{owner}/{repo}/pulls/{number}/diff` — fetch one PR's diff text

Both are scoped to the caller's installation, the same way `/installations/current` already is — every request needs `get_current_installation_id` and a fresh installation token before it can call GitHub at all.

---

## No new manual registration step today

Day 2's GitHub App already has `Pull requests: Read-only` and `Contents: Read-only` permissions, which is everything today's endpoints need. Nothing to change in GitHub's UI.

---

## File 1 — `src/schemas/pull_request.py` — new Pydantic models

- `PullRequestSummary`: `number: int`, `title: str`, `user: InstallationAccount` (reusing the existing `login`-only account shape from `schemas/github_app.py` rather than inventing a near-duplicate), `state: str`, `updated_at: datetime` — the subset of GitHub's PR list response this project actually uses.
- `PullRequestSummaryList = list[PullRequestSummary]` (or a small wrapper model, matching the `RepositoryListResponse` shape already established) — GitHub's PR list endpoint returns a bare JSON array, not an object, so this is worth deciding explicitly rather than improvising per-call.

**Why not reuse `Repository` or `Installation`:** those model different GitHub objects; adding fields to them "because they're kind of similar" would blur what each schema actually represents. A new, small, single-purpose model per external shape is the same discipline Day 2 already established.

---

## File 2 — `src/services/pull_requests.py` — the GitHub calls

- `async def list_open_pull_requests(client, installation_token, owner, repo) -> list[PullRequestSummary]` — `GET /repos/{owner}/{repo}/pulls?state=open`, authenticated with `Authorization: Bearer <installation_token>`, paginated (GitHub caps each page at 30 by default, 100 max via `per_page`; today's scope handles a single `per_page=100` call and defers real cursor-following pagination unless a repo actually needs it)
- `async def fetch_pull_request_diff(client, installation_token, owner, repo, number) -> str` — `GET /repos/{owner}/{repo}/pulls/{number}` with `Accept: application/vnd.github.v3.diff` (GitHub's diff media type — same header-based content negotiation trick as Day 2's `Accept: application/json` on the token exchange, just selecting a different representation of the same resource), returning the raw diff text rather than a parsed model, since a diff is inherently unstructured text, not a shape to validate

Both functions take `client: httpx.AsyncClient` and a plain `installation_token: str` as parameters — no `Settings` object, no cache lookup inside this module. The cache lookup happens once, in the router, using the pattern already proven by `/installations/current`; these two functions shouldn't need to know or care where the token came from.

**Why 404 and rate-limit handling belong here, not later:** GitHub returns `404` for both "this repo doesn't exist" and "this installation can't see this repo" (GitHub deliberately doesn't distinguish the two, to avoid leaking which private repos exist) — that needs to become a typed, expected outcome, not an unhandled exception bubbling up as a generic 500. Rate-limit handling (`403` + `X-RateLimit-Reset`) is explicitly deferred to Day 4's retry/backoff work; today's version lets a rate-limit response surface as an honest error rather than silently retrying — building that correctly is tomorrow's dedicated task, not something to half-do today.

---

## File 3 — `src/routers/pull_requests.py` — the two endpoints

Both endpoints share the same shape:
1. `installation_id: int = Depends(get_current_installation_id)` — same dependency `/installations/current` already uses
2. `installation_token = await installation_token_cache.get_installation_token(client, settings, installation_id)` — the same cached-token lookup, not duplicated logic
3. Call the corresponding `pull_requests.py` function
4. On a GitHub `404`, return FastAPI's own `404` (repo/PR not found or not visible to this installation — same response either way, deliberately, for the reason above)

**Why a new router instead of adding routes to `github_app.py`:** `github_app.py` is specifically the install/auth lifecycle (install, callback, "what can I see"); PR/diff fetching is a different concern that happens to depend on that lifecycle's output. Keeping them separate means a future reviewer can find "how does auth work" and "how do we fetch PR data" in two different, individually-scoped files rather than one growing router mixing both.

---

## .NET parallels

- GitHub's content-negotiation-via-`Accept`-header for the diff format (JSON object vs. raw diff text, same URL) ≈ ASP.NET Core's content negotiation via `Accept` on an action that supports multiple `[Produces]` formats — same HTTP mechanism, same reason to prefer it over separate URLs per format.
- Treating `404` as an expected, typed outcome rather than an exception ≈ returning `NotFound()` from a controller action instead of letting a missing-entity case throw and get caught by global exception middleware — both are about making "doesn't exist" a first-class response, not an error path.
- A raw diff string as a return type (no schema validation) ≈ returning `string` or `FileContentResult` from a controller action instead of wrapping genuinely unstructured content in a DTO that adds nothing.

---

## Automated verification (no real GitHub App credentials needed)

All GitHub responses mocked via `respx`, reusing the same signed-installation-cookie helper Day 2's tests already built:

- `GET /github-app/repos/{owner}/{repo}/pulls` with no installation cookie → `401` (same dependency, same behavior already proven for `/installations/current`)
- A valid installation cookie + mocked PR-list response → `200` with the expected typed list
- A mocked `404` from GitHub (repo not found / not visible to this installation) → this service's own `404`, not a 500
- `GET /github-app/repos/{owner}/{repo}/pulls/{number}/diff` with a mocked diff response → `200` with the raw diff text in the body, and the correct `Accept` header actually sent on the outbound request (asserted via `respx`'s route matching)

## Manual verification (needs the real installed GitHub App)

```bash
# 1. Start the app (installation cookie from Day 2's real install flow
#    should still be valid — 30-day cookie lifetime)
uv run fastapi dev src/main.py

# 2. List PRs on a real repo this installation can see:
curl -b cookies.txt http://localhost:8000/github-app/repos/<owner>/<repo>/pulls

# 3. Fetch a real PR's diff:
curl -b cookies.txt http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<number>/diff

# 4. Confirm a repo this installation was NOT given access to returns 404,
#    not a confusing 403 or 500
```

---

## End-of-day checklist

- [ ] `GET /github-app/repos/{owner}/{repo}/pulls` returns real open PRs for a repo this installation can see
- [ ] `GET /github-app/repos/{owner}/{repo}/pulls/{number}/diff` returns real diff text
- [ ] Both endpoints 401 without a valid installation cookie, matching the existing dependency's behavior
- [ ] A repo/PR GitHub can't find (or this installation can't see) returns this service's `404`, not an unhandled exception
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Typed, single-purpose schemas for each new external shape** (`PullRequestSummary`) continues the same discipline from Day 1/2 — resist the urge to reuse an unrelated existing model just because a couple of fields overlap.
- **A raw string return type for the diff, deliberately not wrapped in a schema,** is a new judgment call for this project: not everything crossing a boundary benefits from being forced into a Pydantic model — unstructured content should stay unstructured, and the "typed boundaries" pattern from `architecture-patterns.md` is about validating *shape where shape exists*, not adding ceremony where it doesn't.
- **404-for-both-"not found"-and-"not visible"** is a genuinely new access-control nuance this project hasn't hit before — the earlier TypeScript project didn't have a resource-visibility boundary of this kind (it operated on documents the caller already had, not an external system selectively hiding resources).
- **Deferring rate-limit handling to a dedicated day (Day 4)** rather than folding a partial version into today's work repeats the same "build backoff/retry correctly and deliberately, not half-done alongside something else" instinct as the equivalent day in the earlier project's Week 1.