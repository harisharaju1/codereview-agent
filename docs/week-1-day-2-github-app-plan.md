# Day 2 Replacement Plan — GitHub App Auth (replacing the OAuth App flow)

## What we're doing and why

The previous commit built GitHub login via a classic **OAuth App** — the user authorizes the app, the app gets a token that acts *as that user*, scoped only by a coarse, bundled scope (`public_repo`). `docs/architecture-patterns.md`'s "Known limitation" note already lays out why that's the wrong long-term shape for this project: no read-only scope exists, and the app can reach anything the authorizing user can reach within that scope, not a deliberately chosen set of repos.

This plan replaces that flow with a **GitHub App**. The identity model is fundamentally different, not just a narrower scope string:

- An OAuth App impersonates a user. A GitHub App **has its own identity**, and acts against an explicit **installation** — a repo owner deliberately chose to install this app on a specific set of repos, with specific, fine-grained permissions (e.g. "Pull requests: Read-only," nothing else).
- There is no single "logged-in user" anymore in the sense Day 2's `/auth/me` implied. What matters instead is: *which installations does this app have, and what can it see through each one.* That's actually a better fit for what this project is — a review bot, not a personal dashboard — and it's a more accurate shape for Week 3's PR-fetching work to build on.

This is being written up as its own plan, not folded into Day 3, because it's a structurally different auth mechanism (JWT signing with a private key, short-lived installation tokens, an installations concept) — worth understanding deliberately, the same way Day 2's OAuth flow was.

---

## Before writing any code: register a GitHub App

Manual step, in GitHub's UI, replacing (not editing) the OAuth App registered for Day 2:

1. Go to `github.com/settings/apps/new` (or an org's equivalent under org settings)
2. Name it (e.g. "codereview-agent-dev") — this name becomes part of its public install URL
3. Homepage URL: `http://localhost:8000`
4. **Webhook**: uncheck "Active" for now — this project doesn't consume webhooks yet (that's later, per the learning plan's Week 4 mention of a webhook-triggered flow)
5. **Permissions** — this is the entire point of the migration, set deliberately narrow:
   - Repository permissions → **Pull requests: Read-only**
   - Repository permissions → **Contents: Read-only** (needed to fetch diff content)
   - Repository permissions → **Metadata: Read-only** (mandatory minimum GitHub requires for any App)
   - Leave every other permission at "No access"
6. **Where can this GitHub App be installed?** → "Only on this account" (your own account, for now)
7. Create the App
8. On the App's settings page: note the **App ID** (a number), and under "Private keys," generate and download a `.pem` private key file
9. **Install the App**: from the App's settings page, click "Install App," choose your account, and select either "All repositories" or specific repos to install it on — this creates the **installation** the rest of this plan depends on

These go into local `.env` (never committed):
```
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY_PATH=./github-app-private-key.pem
```
The `.pem` file itself also stays out of git — add it to `.gitignore` alongside `.env`.

---

## Why the auth mechanics are different, not just the scope

| | OAuth App (previous) | GitHub App (this plan) |
|---|---|---|
| Registered as | Client ID + Client Secret | App ID + a private key (`.pem`), used to sign JWTs |
| Acts as | The authorizing user | The app itself, against a chosen installation |
| Token type | User access token (long-lived, effectively until revoked) | Installation access token (~1 hour, must be refreshed) |
| Permission granularity | Bundled scopes (`repo`, `public_repo` — always read+write together) | Per-resource, per-action (Pull requests: Read-only, etc.) |
| Repo coverage | Everything the authorizing user can access | Explicitly chosen at install time |

The short-lived installation token is the concrete new piece of engineering this plan adds: unlike Day 2's OAuth token (mint once, use indefinitely), this token expires in about an hour and has to be refreshed — a real, small "manage a token's lifecycle" problem worth building correctly rather than glossing over.

---

## File 1 — `src/config/settings.py` changes

Replace the OAuth-specific fields with:
```python
github_app_id: str
github_app_private_key_path: str
```
`github_client_id`, `github_client_secret`, and `github_oauth_redirect_uri` are removed — this is a replacement, not an addition, matching the "second commit replaces the first" plan. `session_secret_key` stays, since signed cookies are still useful (see File 5).

**Why remove rather than keep both:** an unused OAuth App configuration sitting alongside the new one is exactly the "dead configuration that looks load-bearing but isn't" problem flagged back on Day 1 — if a field isn't consumed by anything, it shouldn't be a required setting.

---

## File 2 — `src/schemas/github_app.py` — new Pydantic models

- `InstallationAccessToken`: `token: str`, `expires_at: datetime` — GitHub's response when exchanging a JWT for an installation token
- `Installation`: `id: int`, `account_login: str` — the subset of GitHub's installation object this project actually uses

---

## File 3 — `src/services/github_app_auth.py` — JWT signing + token exchange

- `build_app_jwt(settings: Settings) -> str` — constructs a JWT claiming "I am App `github_app_id`," signed with the private key loaded from `github_app_private_key_path`, using the `RS256` algorithm (via `PyJWT`, with its `cryptography` extra — plain `PyJWT` doesn't include RSA signing support on its own). Claims: `iat` (issued-at, backdated ~60s to tolerate clock drift), `exp` (expires in ~10 minutes — GitHub requires App JWTs to be short-lived), `iss` (the App ID).
- `async def fetch_installation_token(client: httpx.AsyncClient, app_jwt: str, installation_id: int) -> InstallationAccessToken` — `POST https://api.github.com/app/installations/{installation_id}/access_tokens`, authenticated with `Authorization: Bearer <app_jwt>` (not an installation token — this call is what *produces* one)

**Why a JWT at all, when the OAuth flow never needed one:** the JWT is how the app authenticates *as itself* — it's the GitHub App equivalent of the OAuth App's `client_secret`, just asymmetric (signed with a private key GitHub never sees directly, verified against the public key GitHub already has on file for this App) rather than a shared secret sent over the wire.

---

## File 4 — `src/services/installation_token_cache.py` — token lifecycle management

An in-memory dict, `dict[int, InstallationAccessToken]` keyed by `installation_id`. `async def get_installation_token(client, settings, installation_id) -> str`:
1. Check the cache for an existing token for this installation
2. If present and not close to `expires_at` (some safety margin, e.g. 60s), return its `.token` directly
3. Otherwise, mint a fresh app JWT, exchange it for a new installation token, cache it, return it

**Why this needs its own module, not just inlined into whatever calls it:** every future GitHub API call this project makes (Week 3's PR/diff fetching, certainly) needs a valid installation token, and none of those call sites should have to know or care whether the cached token is still fresh — this is the same "isolate a cross-cutting concern behind one reusable function" reasoning as `get_http_client`.

**Same honest in-memory limitation as `session_store.py` before it:** this cache is wiped on restart and doesn't survive multiple processes — acceptable for Week 1's single-operator, single-process reality, not for a real multi-instance deployment later.

---

## File 5 — `src/routers/github_app.py` — replacing `routers/auth.py`

Three endpoints, replacing the previous three:

- **`GET /github-app/install`** — redirects to `https://github.com/apps/<app-slug>/installations/new` (the App's public install page — no `state`/CSRF handling needed here, since nothing sensitive is being exchanged; the user is just being sent to GitHub's own UI to pick repos)
- **`GET /github-app/callback?installation_id=...&setup_action=...`** — GitHub redirects here after install/update completes. Stores `installation_id` in a **signed** cookie (still using `itsdangerous`, same helper as before) — signed because, unlike an OAuth-style `state`, this value *is* meaningfully security-relevant later: it's what every subsequent API call will be scoped to, so tampering with it matters.
- **`GET /github-app/installations/current`** — reads the signed `installation_id` cookie, calls `get_installation_token`, and (for verification purposes) fetches `GET /installation/repositories` using that token to confirm which repos this installation can actually see. This is Day 2's `/auth/me` replacement: not "who is logged in," but "what can this app currently see."

**What gets deleted:** `src/routers/auth.py`, `src/services/github_oauth.py`, `src/dependencies/session.py`'s `get_current_user` (or its body substantially rewritten — see below), and `src/services/session_store.py` in its Day-2 form. The signed-cookie *mechanism* in `dependencies/session.py` (the `_serializer`/sign/verify helpers) is reused, not thrown away — only what it signs changes, from "an opaque session id pointing at a user's token" to "an installation id."

---

## .NET parallels

- Signing a JWT with an RSA private key and having GitHub verify it against the corresponding public key ≈ the same asymmetric-signing pattern behind a JWT bearer token in ASP.NET Core's `AddJwtBearer()` — just here *we're* the one issuing/signing the token, not consuming one someone else issued.
- The installation-token cache with an expiry check ≈ the same pattern as caching an `IMemoryCache` entry for a short-lived external token (e.g. an `IHttpClientFactory`-backed token provider that refreshes an Azure AD token shortly before it expires) — check-if-fresh, refresh-if-not, is a shape that shows up anywhere a short-lived external credential is involved.
- GitHub's own "installations, each with fine-grained permissions" model is conceptually close to Azure AD App Registrations with resource-scoped API permissions, vs. the coarser, user-impersonating shape of classic OAuth.

---

## Automated verification (no real GitHub App credentials needed)

- `build_app_jwt` produces a JWT whose header/payload decode correctly, with the right `iss`/expiry shape (verifiable without needing a real private key — a test-generated RSA keypair works fine)
- `get_installation_token`, mocked via `respx`: a fresh cache miss triggers a token fetch; a cached, non-expired token is returned without a second HTTP call; a cached-but-expired token triggers a refetch
- `/github-app/callback` sets a signed `installation_id` cookie; a tampered cookie is rejected the same way Day 2's session-cookie test proved for the OAuth flow

## Manual verification (needs the real registered GitHub App + `.env` + `.pem`)

```bash
# 1. Start the app
uv run fastapi dev src/main.py

# 2. Visit the install flow
open http://localhost:8000/github-app/install

# 3. On GitHub, confirm/select the install target and repos

# 4. Confirm you land on /github-app/callback with installation_id set,
#    and a signed cookie in the response

# 5. Confirm the installation is actually usable:
curl -b cookies.txt -c cookies.txt http://localhost:8000/github-app/installations/current
# Expect: the real installation's account login + the list of accessible repos

# 6. Wait out (or artificially shorten, for testing) the ~1hr token expiry,
#    confirm a subsequent call transparently mints a fresh token rather
#    than failing
```

---

## End-of-day checklist

- [ ] `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` missing → app fails to start with a clear error
- [ ] `/github-app/install` redirects to the real App's install page
- [ ] A real install completes and lands on `/github-app/callback` with a signed `installation_id` cookie set
- [ ] `/github-app/installations/current` returns the real installation's repos, proving the JWT → installation-token exchange works end-to-end
- [ ] Installation token caching is verified: repeated calls within the token's lifetime don't re-mint; an expired one does
- [ ] `src/routers/auth.py`, `src/services/github_oauth.py`, and Day 2's OAuth-specific settings fields are removed, not left dangling
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **The signed-cookie mechanism itself (`itsdangerous`, `_serializer`) is reused unchanged from the OAuth Day 2 work** — a good example of a well-isolated piece of infrastructure surviving a fairly large surrounding redesign untouched, because it was built as a generic "sign/verify a value" helper rather than something OAuth-specific.
- **The token-cache-with-expiry-check pattern (`installation_token_cache.py`) is new to this project**, but it's the same general shape as any short-lived-credential cache — the kind of pattern that'll likely reappear once this project starts calling the Claude API in Week 2-3, if that ever involves a refreshable credential rather than a static API key.
- **This is the first time a piece of this project's own Week 2 (Day 2) work has been substantially replaced, not just extended** — worth noting as a real example of "the first design wasn't wrong to build, but building it surfaced exactly why a different mechanism was needed," which is itself a legitimate engineering story, not a false start to be embarrassed about.
- **Registering a GitHub App is the same "manual step in an external UI, can't be automated" shape** as registering the original OAuth App, and as registering AWS/Bedrock access in an earlier project — a recurring category of setup step, not a coincidence.
