# Day 2 (Replacement) — GitHub App Auth

This replaces the classic GitHub OAuth App login built earlier in Day 2 with a GitHub App install flow. See `week-1-day-2-github-app-plan.md` for the plan this followed and the full OAuth-App-vs-GitHub-App comparison table; this doc records what was actually built and verified.

## What was built

**Registration (manual, in GitHub's UI):** a GitHub App (`codereview-agent-dev`) with Pull requests, Contents, and Metadata all set to Read-only, no webhook, installable only on the owning account. This produced an App ID, a downloaded `.pem` private key, and — after installing the app on the account — a real installation to test against.

**`src/config/settings.py`:** `github_client_id`/`github_client_secret`/`github_oauth_redirect_uri` removed; replaced with `github_app_id`, `github_app_private_key_path`, `github_app_slug`. `session_secret_key` unchanged.

**`src/schemas/github_app.py`:** `InstallationAccessToken`, `InstallationAccount`, `Installation`, `Repository`, `RepositoryListResponse` — typed shapes for every GitHub response this flow touches.

**`src/services/github_app_auth.py`:** `build_app_jwt(settings)` signs a short-lived (~10 min) RS256 JWT with the App's private key, `iss` set to the App ID. `fetch_installation_token` and `fetch_installation` call GitHub's installation endpoints authenticated with that JWT.

**`src/services/installation_token_cache.py`:** an in-memory `dict[int, InstallationAccessToken]` cache with a 60-second refresh margin — a cache hit returns the cached token directly; a miss or near-expiry token mints a fresh JWT, exchanges it, and caches the result.

**`src/dependencies/installation.py`:** signs/verifies the `installation_id` cookie (same `itsdangerous` mechanism as before, different salt) and provides `get_current_installation_id` as a `Depends()`-injectable dependency, 401ing on anything missing, tampered, or expired.

**`src/routers/github_app.py`:** `GET /github-app/install` (redirect to the App's install page), `GET /github-app/callback` (signs and sets the installation cookie), `GET /github-app/installations/current` (fetches installation info + accessible repos using a cached installation token).

**Deleted:** `src/routers/auth.py`, `src/services/github_oauth.py`, `src/services/session_store.py`, `src/dependencies/session.py`, `src/schemas/auth.py`, `tests/test_auth.py` — a clean replacement, not a parallel path.

## Why

The OAuth App's only scopes (`repo`, later narrowed to `public_repo`) were both too broad (bundled write access, no read-only option) and, in the narrowed case, too narrow (public repos only). Neither was viable once the goal became "other users install this against their own repos" rather than "the developer logs in." A GitHub App has its own identity, is installed deliberately against a chosen set of repos, and supports genuinely fine-grained, read-only permissions — the actual fix, not a smaller version of the same problem. Full reasoning and the OAuth-vs-App comparison table live in `week-1-day-2-github-app-plan.md` and `architecture-patterns.md`.

## Python-specific notes

- `PyJWT`'s `[crypto]` extra pulls in `cryptography` for RS256 signing — plain `PyJWT` only supports symmetric (HMAC) algorithms out of the box.
- Test fixtures generate a real (but throwaway) RSA keypair via `cryptography.hazmat.primitives.asymmetric.rsa` rather than hardcoding a fake key — this is both simpler than maintaining a checked-in test fixture key and avoids ever having a "looks like a real private key" string sitting in the repo, even a fake one.
- `respx_mock` (installed via `respx`) is available as a fixture automatically once the package is installed — no explicit import or decorator required, unlike some other HTTP-mocking libraries.

## .NET parallel

Signing a JWT with an RSA private key and having the counterparty verify it against the corresponding public key is the same asymmetric-signing shape behind ASP.NET Core's `AddJwtBearer()` — the difference here is that this service is the *issuer*, not the *consumer*, of the token. The installation-token cache (check freshness, refetch if stale) is the same shape as caching a short-lived Azure AD token behind an `IHttpClientFactory`-backed token provider.

## Verified

**Automated** (`uv run pytest`, all green):
- `build_app_jwt` produces a JWT with the correct `iss` and a valid `exp > iat` window
- Installation token caching: a cache miss fetches and caches; a stale cached token triggers a refetch; only one HTTP call happens per distinct fetch
- `/github-app/install` redirects to the correct install URL
- `/github-app/callback` sets a signed cookie; `/github-app/installations/current` 401s with no cookie or a tampered one, and returns installation + repo data end-to-end with a valid one (all via `respx`-mocked GitHub responses)

**Manual, with real credentials:**
- Confirmed the real `.pem` file (not just the test-generated one) produces a valid RS256-signed JWT with `iss` equal to the real App ID
- `/health`, `/github-app/install` (redirects to the real App's install URL using `GITHUB_APP_SLUG`), and unauthenticated `/github-app/installations/current` (401) all verified against the running server
- Full browser install flow completed: installed the app on the real account, landed on `/github-app/callback` with a signed cookie set, and `/github-app/installations/current` returned the real installation's account and accessible repositories

**Not yet exercised:** the ~1 hour installation-token natural expiry-and-refresh in a live run (covered by the mocked unit test, but not observed against real GitHub timing) — acceptable to leave unverified manually since the unit test already proves the refresh logic itself is correct.

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- The signed-cookie mechanism itself (`itsdangerous`, sign/verify with a salt) survived a fairly large surrounding redesign untouched, because it was built as a generic "sign/verify a value" helper rather than something OAuth-specific — the same "isolate the genuinely reusable piece" instinct as `get_http_client` from Day 1.
- The token-cache-with-expiry-check pattern (`installation_token_cache.py`) is new to this project, but it's the same general shape as any short-lived-credential cache — likely to reappear once this project calls an LLM API in Week 2-3, if that ever involves a refreshable credential rather than a static key.
- This is the first time a piece of this project's own earlier work was substantially replaced, not just extended — a legitimate "the first design wasn't wrong to build, but building it surfaced exactly why a different mechanism was needed" story, matching the kind of mid-course correction that showed up at least once in the prior month's project too.
- Registering a GitHub App is the same "manual step in an external UI, can't be automated" shape as registering the original OAuth App — a recurring category of setup step across projects, not a coincidence.
