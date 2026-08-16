# Day 2 Plan — GitHub OAuth Flow

## What we're doing and why

Day 1 gave us a running skeleton with nothing behind it. Day 2 adds the first real feature: letting a user log in with their GitHub account, so later days can make GitHub API calls "as" that user rather than with some hardcoded token.

Three endpoints, all in a new `auth` router:
- `GET /auth/github/login` — kicks off the OAuth flow, redirects to GitHub
- `GET /auth/github/callback` — GitHub redirects back here with a code; we exchange it for a token, fetch the user, and start a session
- `GET /auth/me` — returns whoever's logged in, so we have something to check against without waiting on Day 3's PR endpoints

Two security details that aren't optional: a `state` value that proves the callback really followed from a login *we* initiated (CSRF protection), and never putting the raw GitHub access token somewhere the browser can read it.

---

## Before writing any code: register a GitHub OAuth App

This is a manual step in GitHub's UI — it can't be automated:

1. Go to `github.com/settings/developers` → "OAuth Apps" → "New OAuth App"
2. Application name: anything (e.g. "codereview-agent-dev")
3. Homepage URL: `http://localhost:8000`
4. Authorization callback URL: `http://localhost:8000/auth/github/callback`
5. Save, then copy the **Client ID**, and generate + copy a **Client Secret**

These go into a local `.env` file (never committed):
```
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
SESSION_SECRET_KEY=<any long random string>
```

Automated tests today don't need real credentials at all (GitHub's API is mocked). Only the manual browser verification step at the end needs the real registered app.

---

## File 1 — `src/config/settings.py` additions

```python
github_client_id: str
github_client_secret: str
github_oauth_redirect_uri: str = "http://localhost:8000/auth/github/callback"
session_secret_key: str
```

**Why these are required now, with no default:** this is exactly the moment Day 1's note about not stubbing dead config was pointing at — these fields are required *because* the OAuth code landing today genuinely can't function without them. Missing any of them now correctly crashes the app at startup with a clear message, instead of failing confusingly the first time someone clicks "log in."

---

## File 2 — `src/schemas/auth.py` — Pydantic models

- `GitHubTokenResponse`: `access_token: str`, `token_type: str`, `scope: str` — the shape of GitHub's token-exchange response
- `GitHubUser`: `login: str`, `id: int`, `name: str | None`, `avatar_url: str | None` — the subset of GitHub's `/user` response we actually care about

**Why model GitHub's responses explicitly, rather than passing raw dicts around:** the same reasoning as Day 1's schema/type unification — if GitHub ever changes a field name or we typo one, this fails loudly at the boundary (a validation error) instead of silently producing `None` deep inside some unrelated piece of logic.

---

## File 3 — `src/services/session_store.py` — server-side session storage

An in-memory dict, module-level: `_sessions: dict[str, SessionData]`, where `SessionData` holds `github_access_token: str` and `user: GitHubUser`. Functions: `create_session(access_token, user) -> str` (generates an opaque session id via `secrets.token_urlsafe(32)`, stores it, returns it), `get_session(session_id) -> SessionData | None`.

**Why the access token never goes in the cookie itself:** a cookie can be *signed* (tamper-evident) without being *encrypted* (unreadable). A signed-but-unencrypted cookie containing the real GitHub access token would still let the browser (or anyone who can read the browser's cookie jar) read that token in plain text — signing only proves we wrote it, it doesn't hide it. So the cookie carries only a random, meaningless session id; the actual sensitive token lives server-side, keyed by that id, and is never sent to the client at all.

**Honest limitation, worth stating plainly:** this dict is in-process memory — it's wiped on every restart, and won't work across multiple server processes/replicas. That's fine for a single-operator personal project right now; a real multi-instance deployment would need this backed by something shared (Redis, a database) instead. Not a Week 1 problem.

---

## File 4 — `src/services/github_oauth.py` — the OAuth mechanics

- `build_authorize_url(state: str) -> str` — constructs GitHub's authorize URL with `client_id`, `redirect_uri`, `scope=public_repo`, and `state`

  *(Revised after this plan was first written: the original plan called for `scope=repo`, which grants full read/write on private repos plus org-management access this service has no use for. Narrowed to `public_repo` — see `docs/architecture-patterns.md`'s "Known limitation" note for the full reasoning, and the follow-up GitHub App migration this doesn't fully solve.)*
- `async def exchange_code_for_token(code: str) -> GitHubTokenResponse` — `POST https://github.com/login/oauth/access_token` with `client_id`, `client_secret`, `code`, `redirect_uri`, `Accept: application/json` (GitHub defaults to a form-encoded response without this header — an easy, silent mistake if skipped), validated into `GitHubTokenResponse`
- `async def fetch_github_user(access_token: str) -> GitHubUser` — `GET https://api.github.com/user` with `Authorization: Bearer <token>`, validated into `GitHubUser`

Both functions take an `httpx.AsyncClient` as a parameter rather than constructing their own — this is what makes them mockable in tests (Day 3's PR/diff fetching will reuse the same injected-client pattern, so this is being set up once, correctly, rather than redone).

---

## File 5 — `src/dependencies/http_client.py` — shared async client

A FastAPI dependency that provides a single `httpx.AsyncClient`, created once at app startup (via a lifespan handler) and reused across requests, rather than a new client per request. Connection pooling is the whole point of reusing a client — opening a fresh TCP+TLS connection to GitHub on every single call is wasteful and slower.

**Why this belongs in Day 2, even though the original week plan filed it under Day 3:** OAuth's token exchange and user fetch already need to call GitHub over HTTP today. Standing up the shared-client dependency now means Day 3's PR/diff endpoints just depend on the same thing that already exists, instead of this being built twice.

---

## File 6 — `src/routers/auth.py` — the three endpoints

**`GET /auth/github/login`:**
1. Generate `state = secrets.token_urlsafe(32)`
2. Set it in a short-lived (10 min), `httponly`, `samesite=lax` cookie named `oauth_state` — unsigned is fine here, since the value itself is already a high-entropy random token an attacker can't guess or reconstruct
3. Redirect (307) to `build_authorize_url(state)`

**`GET /auth/github/callback?code=...&state=...`:**
1. Read the `oauth_state` cookie; if missing, or it doesn't match the `state` query param, return `400` — this is the CSRF check, and it's not optional
2. Delete the `oauth_state` cookie (single use)
3. `exchange_code_for_token(code)` → `fetch_github_user(access_token)`
4. `create_session(access_token, user)` → get a session id
5. Sign the session id (via `itsdangerous.URLSafeTimedSerializer`, keyed by `session_secret_key`) into a cookie named `session` — `httponly`, `samesite=lax`, a few days' expiry
6. Return the `GitHubUser` as JSON

**`GET /auth/me`:** depends on a `get_current_user` dependency that reads the `session` cookie, verifies the signature and expiry, looks up the session id in the store, and returns `401` if any of that fails — otherwise returns the stored `GitHubUser`.

**Why `state` is checked as an exact cookie-vs-query-param match, not something fancier:** this is the standard OAuth CSRF defense (RFC 6819) — the value only needs to be unguessable and tied to the browser that initiated the login; a signature on top of it wouldn't add real protection here; the session cookie, by contrast, is worth signing since a forged session id would grant access to *someone else's* stored token if it weren't authenticated.

---

## .NET parallels

- The OAuth code-exchange dance (login → redirect → callback → token exchange) is the same shape as ASP.NET Core's `AddOAuth`/`AddGitHub` authentication handler — we're just hand-rolling what a library normally does, which is the point this week (understanding the mechanics, not hiding behind a package).
- `itsdangerous.URLSafeTimedSerializer` signing a cookie ≈ ASP.NET Core Data Protection signing/encrypting an auth cookie under the hood — same idea (tamper-evident, time-limited token), different library.
- The in-memory `_sessions` dict is the rough equivalent of ASP.NET Core's in-memory `IDistributedCache`/session provider — fine for one process, and the same "this doesn't survive a restart or scale past one instance" caveat applies to both.
- A dependency-provided, lifespan-managed `httpx.AsyncClient` ≈ registering `HttpClient` via `IHttpClientFactory` in `Program.cs` — both exist specifically to avoid the well-known "a new client per call exhausts sockets / defeats connection pooling" problem.

---

## Automated verification (no real GitHub credentials needed)

All of GitHub's HTTP responses are mocked via `respx`, so these run in CI/locally without a registered OAuth app:

- `GET /auth/github/login` → `307` redirect to a URL containing the right `client_id`, `redirect_uri`, `scope`, and a `state` param; response sets an `oauth_state` cookie
- `GET /auth/github/callback` with a `state` that doesn't match the cookie → `400`, no session cookie set
- `GET /auth/github/callback` with a matching `state` and mocked GitHub responses → `200` with the expected user JSON, and a `session` cookie is set
- `GET /auth/me` with no cookie → `401`
- `GET /auth/me` with a valid session cookie (obtained from a successful mocked callback in the same test) → `200` with the logged-in user

## Manual verification (needs the real registered OAuth app + `.env`)

```bash
# 1. Start the app
uv run fastapi dev src/main.py

# 2. In a browser, visit:
open http://localhost:8000/auth/github/login

# 3. Approve the GitHub authorization prompt

# 4. Confirm you land back on /auth/github/callback with your real
#    GitHub user JSON in the response

# 5. Confirm the session persists:
curl -b cookies.txt -c cookies.txt http://localhost:8000/auth/me
# (capture cookies from the browser dev tools, or test via browser directly)

# 6. Tamper test: manually edit one character of the session cookie value,
#    confirm /auth/me now returns 401 (proves the signature check works)
```

---

## End-of-day checklist

- [ ] `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` / `SESSION_SECRET_KEY` missing from env → app fails to start with a clear error
- [ ] `/auth/github/login` redirects to GitHub with correct query params and sets `oauth_state`
- [ ] A mismatched `state` on callback is rejected with `400`
- [ ] A real browser login round-trip succeeds end-to-end against the registered OAuth app
- [ ] `/auth/me` correctly returns `401` with no session, and the logged-in user with a valid one
- [ ] Tampering with the session cookie's contents is detected and rejected
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Typed, validated response models for an external API's payloads** (`GitHubTokenResponse`, `GitHubUser`) is the same instinct as the earlier project's Zod schemas around the LLM's structured output — never trust an external response's shape by convention alone, validate it at the boundary.
- **A dependency-injected, reused HTTP client** is a new pattern this project needs that the earlier project didn't — that one talked to Claude/Bedrock via their own SDKs, which manage connection reuse internally; here we're calling a plain REST API ourselves, so the "don't create a new client per call" lesson has to be learned explicitly rather than being handled for us.
- **The signed-cookie / session-id split** (opaque id in the cookie, real secret server-side) is a genuinely new concept for this project — the earlier project had no concept of a logged-in user or session at all, since it was called machine-to-machine, not by a human logging in through a browser.
- **The "manual step in an external UI that can't be automated" pattern** (registering the OAuth App here; registering AWS credentials/Bedrock access in the earlier project) is a repeat of a shape we've hit before: some setup steps are inherently outside what any amount of code can do for you.
