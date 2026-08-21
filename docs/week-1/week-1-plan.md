# Week 1 — Python Backend Foundations & GitHub Integration

## What This Project Is

An AI-powered code review agent: a service that connects to a GitHub repo, reads pull request diffs, and produces structured review feedback — security issues, performance concerns, style violations, suggestions. Over the coming weeks it grows from a plain API into a multi-step tool-using agent, then a LangGraph-based agent, then an MCP server that any MCP-compatible client (Claude Desktop, Claude Code CLI, Cursor, etc.) can call directly.

Week 1 is purely the foundation: a real Python/FastAPI backend, structured the way a production service should be, and a working GitHub integration — OAuth login, fetching PRs, fetching diffs. No review logic yet; that starts Week 2.

By the end of the week: a running FastAPI service where a real GitHub OAuth login works end-to-end in a browser, and — once logged in — the service can list a repo's open PRs and fetch a given PR's diff text, with retries and typed error handling around every GitHub API call.

---

## Decisions Made Up Front

- **Package manager: `uv`.** Handles the virtual environment, dependency locking, and script running in one tool — the fast, modern default in the Python ecosystem right now.
- **Project layout:**
  ```
  src/
    routers/        # FastAPI routers — thin: parse input, call a service, shape the response
    services/       # business logic (GitHub client, OAuth exchange, diff fetching)
    schemas/        # Pydantic models — request/response shapes and GitHub API response shapes
    dependencies/   # FastAPI dependency-injection providers (settings, HTTP client, token store)
    config/         # env loading + validation
    main.py         # app entrypoint
  ```
- **GitHub client: `httpx`, used async.** Fits FastAPI's async request model directly, and any friction between Python's `asyncio` model and event-loop-based async elsewhere is worth hitting honestly here rather than sidestepping it with a synchronous client.
- **Full OAuth App flow**, not a personal access token. Even though only one person uses this service right now, the OAuth login/callback exchange is being built as the real thing from the start.
- **Token storage for Week 1**: an in-memory dict keyed by a session id, with a signed cookie carrying that session id. It's the simplest thing that demonstrates a correct OAuth flow. Durable token storage is a natural upgrade once a database is part of this project — not required this week.
- **Testing**: `pytest` + `pytest-asyncio` for async test support + `respx` for mocking `httpx` calls to GitHub's API without hitting the network.

## One Manual Step Required Before Day 2

Register a GitHub OAuth App at `github.com/settings/developers` to get a Client ID and Client Secret. Set the callback URL to `http://localhost:8000/auth/github/callback` for local development (a production callback URL gets added once this service is deployed). This step can't be automated — it happens in the GitHub UI.

---

## Day 1 — FastAPI Project Scaffolding (~2-3 hrs)

**Build:**
- `uv init`, set up the `src/` layout above, `pyproject.toml`
- FastAPI app with `GET /health` returning `{"status": "ok"}`
- Env config via `pydantic-settings`, validated at startup — the process should fail fast with a clear error if a required env var (e.g. `GITHUB_CLIENT_ID`) is missing, not fail on first use
- `pytest` wired up with one trivial passing test (e.g. env validation: a valid env passes, a missing key raises)
- Ruff configured for linting and formatting

**Concept:** The routers/services/schemas split keeps HTTP concerns (parsing, status codes) separate from business logic, and Pydantic models double as both the runtime validation layer and the type system — the same job Zod does in a TypeScript project. Validating env vars at startup, not on first request, is a habit worth having from day one: a process that crashes immediately with "missing GITHUB_CLIENT_ID" is far easier to debug than one that fails 10 minutes later on the first real login attempt.

---

## Day 2 — GitHub OAuth Flow (~3 hrs)

**Build:**
- `GET /auth/github/login` — redirects to GitHub's OAuth authorize URL with `client_id`, requested scope (`repo`, read access to pull requests), and a `state` parameter for CSRF protection
- `GET /auth/github/callback` — receives the authorization code, exchanges it for an access token via GitHub's token endpoint, verifies the `state` param matches, stores the token against a session, sets a signed session cookie
- Pydantic schemas for GitHub's token-exchange response and the authenticated user object
- Manual verification: log in through a real browser against the registered OAuth App, confirm the token exchange succeeds and a tampered/mismatched `state` is rejected

**Concept:** OAuth's `state` parameter exists specifically to prevent CSRF attacks against the callback — an attacker tricking a logged-in user's browser into completing an OAuth flow they didn't initiate. Verifying it isn't optional plumbing; skipping it is a real, exploitable gap. This is also the first place session identity shows up in this project — everything from Day 3 onward (fetching PRs "as" a logged-in user) depends on this working correctly.

---

## Day 3 — Fetch PRs and Diffs (~3 hrs)

**Build:**
- `GET /repos/{owner}/{repo}/pulls` — authenticated call to GitHub's PR list endpoint, paginated, returning Pydantic-modeled PR summaries (number, title, author, state, updated_at)
- `GET /repos/{owner}/{repo}/pulls/{number}/diff` — fetch the actual diff text for a given PR (GitHub's diff media type)
- An async `httpx` client wired up as a FastAPI dependency, so it's created once and reused/testable rather than instantiated per-request
- Explicit, typed handling of GitHub API error cases — repo/PR not found (404), rate-limited (403) — returned as typed error responses, not unhandled exceptions that bubble up as a generic 500

**Concept:** A dependency-injected HTTP client is the FastAPI-idiomatic way to share a resource (connection pooling, auth headers) across requests without global state. Treating "not found" and "rate limited" as expected, typed outcomes rather than exceptions is the same production habit as any external API call: the caller of this service needs to be able to tell "this PR doesn't exist" apart from "something broke," and a stack trace doesn't communicate that distinction.

---

## Day 4 — Retry/Backoff and Async Patterns (~3 hrs)

**Build:**
- Hand-written retry-with-exponential-backoff-and-jitter around the GitHub client calls — capped attempts, handling GitHub's rate-limit response (403 plus an `X-RateLimit-Reset` header telling you exactly when to retry) separately from a transient 5xx
- Unit tests using `respx`-mocked GitHub responses: a successful fetch, a 404 handled as a typed failure, and a rate-limit-then-success retry path
- A written note on the concrete friction points hit moving from Node's single-threaded event loop model to Python's `asyncio` — where the mental model transfers cleanly and where it doesn't

**Concept:** Writing backoff-with-jitter by hand once, rather than reaching straight for a library, is what actually teaches why jitter matters (uncoordinated retries from multiple clients hitting the same rate limit at the exact same backoff intervals just recreate the thundering-herd problem backoff was supposed to solve). GitHub's rate-limit response is a good real-world case for "the server tells you exactly when to retry" — respecting `X-RateLimit-Reset` instead of blind exponential backoff is the correct behavior when a service is explicit about it.

---

## Day 5 — Infrastructure: Docker Compose + Deploy (~2 hrs)

**Build:**
- `docker-compose.yml` for this project
- Multi-stage `Dockerfile` — a `uv`-based build stage, then a slim runtime stage
- `.env.example` and `.dockerignore`
- Deploy and confirm `/health` is reachable through the reverse proxy

**Concept:** A multi-stage build keeps the runtime image free of build tooling (compilers, the full `uv` cache) that has no business being in a production container — smaller image, smaller attack surface.

---

## End-of-Week Checkpoint

1. The service serves `GET /health`
2. A real GitHub account can complete the full OAuth login flow in a browser, landing on an authenticated session
3. Given a real repo and PR number, the service returns PR metadata and the actual diff text
4. GitHub API calls are wrapped in retry/backoff, unit-tested against mocked failure and rate-limit responses
5. `pytest` passes end-to-end, covering env validation, PR/diff fetch error handling, and retry/backoff logic

If Day 5 (infra) slips, prioritize 1-4 running locally and carry the deploy step into the following week rather than compressing the OAuth or retry-logic work.

---

## Learning Notes: Similarities to Prior Work

This section is a private cross-reference for tracking how this week's work relates to earlier projects and the broader learning plan — it doesn't affect anything above, which stands on its own as this project's plan.

- **Structural pattern reused directly**: the `routers/services/schemas` split mirrors the `routes/services/schemas` split from the previous month's TypeScript project almost one-for-one — same separation of concerns (thin HTTP layer, business logic, validation-as-types), just FastAPI/Pydantic idiom instead of Fastify/Zod.
- **Fail-fast env validation at startup** is the same habit carried over from that earlier project's Day 1, just swapping Zod for `pydantic-settings`.
- **Hand-written retry-with-backoff-and-jitter, capped attempts, tested with mocked failures** is a direct repeat of a pattern built in that earlier project's Week 1 — same reasoning (understand the backoff math before reaching for a library), applied again against a different upstream (GitHub's API instead of an LLM API), and this time also needing to respect a rate-limit-reset header, which the earlier version didn't have to handle.
- **New this time, not present in the earlier project**: the OAuth login/callback flow itself (the earlier project had no user-facing auth at all), and the asyncio-vs-Node-event-loop friction — a genuinely new mental-model gap rather than a repeated pattern.
- **Docs rhythm itself** (week plan up front, day-by-day docs after, README once far enough along) is the same convention carried forward from the earlier project, now made an explicit standing convention for this and future projects rather than something that just happened to be done that way once.
