# Day 2 — GitHub OAuth Flow

## What was built

- **`src/config/settings.py` additions** — `github_client_id`, `github_client_secret`, `github_oauth_redirect_uri` (defaulted to the local callback URL), `session_secret_key`, all required with no default.
- **`src/schemas/auth.py`** — `GitHubTokenResponse` (GitHub's token-exchange response) and `GitHubUser` (the subset of GitHub's `/user` response this service cares about).
- **`src/services/session_store.py`** — an in-memory `dict[str, SessionData]`, `create_session`/`get_session`, session ids generated via `secrets.token_urlsafe(32)`.
- **`src/services/github_oauth.py`** — `build_authorize_url(state)`, `exchange_code_for_token(code)`, `fetch_github_user(access_token)`, all taking an injected `httpx.AsyncClient` rather than constructing one.
- **`src/dependencies/http_client.py`** — a single lifespan-managed `httpx.AsyncClient`, shared across requests via `Depends()`.
- **`src/routers/auth.py`** — `GET /auth/github/login` (redirect to GitHub with `state`), `GET /auth/github/callback` (state check, code exchange, session creation, signed session cookie), `GET /auth/me` (depends on `get_current_user`, 401s without a valid session).
- **`tests/test_auth.py`** — full flow tested against mocked GitHub responses: state mismatch, successful login, session persistence, tampered-cookie rejection.

## Why it's built this way

- **`state` as an unsigned, high-entropy, single-use cookie** — the standard OAuth CSRF defense. It only needs to be unguessable and tied to the browser that started the flow; a signature wouldn't add real protection on top of that.
- **The session cookie carries an opaque, signed session id — never the real GitHub access token.** Signing proves the cookie wasn't tampered with; it doesn't hide the value, so anything actually sensitive (the access token) stays server-side in `session_store.py`, keyed by that id.
- **`http_client.py` landed a day earlier than the original week plan called for.** OAuth's token exchange and user fetch already need to call GitHub over HTTP on Day 2, so standing up the shared-client dependency now means Day 3's PR/diff endpoints reuse something that already exists instead of building it twice.
- **The originally planned `scope=repo` was narrowed to `scope=public_repo` after registering the app and reviewing GitHub's actual consent screen**, which revealed `repo` bundles org-management access this service has no use for. `public_repo` is a stopgap, not a full fix — see `docs/architecture-patterns.md` for why, and `docs/week-1-day-2-github-app.md` for the real fix that replaced this flow entirely.

## Python-specific things worth calling out

- **`itsdangerous.URLSafeTimedSerializer`** signs a value and embeds an expiry check via `max_age`, so a cookie past its lifetime fails verification the same way a tampered one does — expiry enforcement doesn't need to be hand-rolled separately.
- **`secrets.token_urlsafe(32)` vs. `secrets.compare_digest`** — token *generation* uses a CSPRNG (`token_urlsafe`); token *comparison* (checking the callback's `state` against the cookie) uses a constant-time comparison (`compare_digest`) specifically to avoid leaking timing information about how much of the string matched.
- **`Depends()` used for the shared `httpx.AsyncClient`** is the same DI mechanism as `get_settings` from Day 1, just providing a different kind of shared resource (a network client with connection pooling, rather than a config object).

## .NET parallel

- The OAuth code-exchange dance (login → redirect → callback → token exchange) is the same shape as ASP.NET Core's `AddOAuth`/`AddGitHub` handler — hand-rolled here deliberately, to actually learn the mechanics rather than hide behind a package.
- `itsdangerous` signing a cookie ≈ ASP.NET Core Data Protection signing/encrypting an auth cookie under the hood.
- The in-memory `_sessions` dict ≈ an in-memory `IDistributedCache`/session provider — same single-process, doesn't-survive-a-restart caveat on both sides.
- A lifespan-managed, DI-provided `httpx.AsyncClient` ≈ `HttpClient` registered via `IHttpClientFactory` — both exist to avoid the well-known "new client per call defeats connection pooling" problem.

## Verified manually

- `GET /auth/github/login` redirects to GitHub with the correct `client_id`, `redirect_uri`, `scope`, and `state`, and sets the `oauth_state` cookie.
- A real browser login round-trip against the registered OAuth App completed successfully, landing on `/auth/github/callback` with the real GitHub user returned as JSON.
- `GET /auth/me` returned the logged-in user with a valid session cookie, and `401` with none.
- Tampering with one character of the session cookie's value caused `/auth/me` to return `401` — confirming the signature check actually rejects modified values, not just missing ones.
- `uv run pytest` and `uv run ruff check .` both clean.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Typed, validated response models for an external API's payloads** (`GitHubTokenResponse`, `GitHubUser`) is the same instinct as the earlier project's Zod schemas around the LLM's structured output — validate an external response's shape at the boundary, never trust it by convention.
- **A dependency-injected, reused HTTP client** is a pattern this project needed that the earlier project didn't — that one talked to Claude/Bedrock through their own SDKs, which manage connection reuse internally; here, calling a plain REST API directly means the "don't create a new client per call" lesson has to be learned explicitly.
- **The signed-cookie / session-id split** (opaque id in the cookie, real secret server-side) is a genuinely new concept for this project — the earlier project had no logged-in user or session at all, since it was called machine-to-machine.
- **The "manual step in an external UI that can't be automated" pattern** (registering the OAuth App here; registering AWS/Bedrock access in the earlier project) is a repeat of a shape hit before: some setup steps are inherently outside what code can do for you.
- **This flow was replaced days later by a GitHub App** (see `docs/week-1-day-2-github-app.md`) — worth remembering this doc describes what was built and why it made sense *at the time*, not the current state of the codebase.
