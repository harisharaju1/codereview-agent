# Architecture & Design Patterns

A running catalogue of the structural patterns this project uses and why — updated at the end of each week's work, not each day's, so it reflects settled decisions rather than in-progress ones. Day-level reasoning and tradeoffs live in the individual `week-N-day-M.md` docs; this file is the higher-altitude view across the whole codebase.

---

## Week 1 — Python Backend Foundations & GitHub OAuth

### Layered architecture: routers → services → schemas/dependencies

The codebase is split into four kinds of modules, each with one job:

- **`routers/`** — HTTP-facing only. Parse the request, call into a service function, shape the response. A router should never contain business logic itself.
- **`services/`** — the actual logic (`github_oauth.py`, `session_store.py`). These are plain functions/modules with no FastAPI-specific code in them at all — they could be called from a CLI script or a test with no HTTP involved.
- **`schemas/`** — Pydantic models describing the shape of data crossing a boundary (a request/response body, or an external API's response). A schema is both a type and a runtime validator at once.
- **`dependencies/`** — FastAPI-specific wiring that *provides* something to a route (the shared HTTP client, the current authenticated user) via the `Depends(...)` mechanism, sitting between routers and services.

The reason for the split: it keeps each piece independently testable and independently reasoned-about. A router change (add a new endpoint) shouldn't require touching business logic; a business-logic change (how we validate an OAuth state) shouldn't require touching HTTP-handling code.

### Dependency injection via `Depends(...)`

Nothing in this codebase constructs its own dependencies (an HTTP client, the settings object, the current user) — every route declares what it needs as a function parameter with a `Depends(...)` default, and FastAPI resolves and injects it at request time. This has two concrete payoffs already visible in Week 1:

- **Shared, reused resources.** `get_http_client` hands every route the *same* `httpx.AsyncClient` (created once at app startup), rather than each call site constructing its own — the difference between reusing a connection pool and paying for a fresh TCP+TLS handshake on every request.
- **Composable auth.** `get_current_user` is "the logged-in user, or a 401" as a single reusable dependency. Any future route that needs an authenticated caller (Day 3's PR/diff endpoints) just adds `Depends(get_current_user)` — the check isn't re-implemented or copy-pasted per route.

### Fail-fast configuration

`Settings` (a `pydantic-settings` `BaseSettings` subclass) declares every environment variable the app needs, with no default for the ones that are genuinely required (`github_client_id`, `github_client_secret`, `session_secret_key`). Missing one of these crashes the app immediately on startup with a clear validation error, rather than the app starting successfully and failing confusingly on the first request that happens to need it.

A deliberate related discipline: a config field is only added to `Settings` in the same day's work that actually starts consuming it (GitHub OAuth fields landed on Day 2, alongside the OAuth code, not stubbed in on Day 1 when nothing used them yet). The alternative — adding fields "for later" — produces required configuration that looks load-bearing but isn't, which is its own source of confusion.

### Signed opaque session identifiers, not raw secrets, in cookies

The browser-facing session cookie never contains the real GitHub access token. It contains a random, meaningless session id, signed (via `itsdangerous`) so tampering is detectable. The actual access token lives server-side, in a store keyed by that session id.

This is a general pattern, not specific to GitHub OAuth: **signing proves a value wasn't altered; it does not hide the value.** Anything genuinely sensitive belongs server-side, referenced by an opaque handle the client carries — never embedded directly in a client-readable cookie, signed or not.

### Typed boundaries around external data

Every external response this service parses (GitHub's token-exchange response, GitHub's user object) is validated into a Pydantic model before anything else touches it, rather than passed around as a raw `dict`. A shape mismatch — GitHub renaming a field, or a bug constructing a mocked response in tests — fails loudly and immediately at the boundary, instead of surfacing later as a confusing `None` or `KeyError` deep in unrelated logic.

### Testability by construction, not by accident

Two choices exist specifically to make the OAuth flow unit-testable without hitting the real GitHub API:

- `github_oauth.py`'s functions take an `httpx.AsyncClient` as a parameter rather than constructing one internally — this is what lets tests substitute a mocked client (via `respx`) transparently.
- The session and OAuth-state stores are read/written through plain functions (`create_session`, `get_session`), not baked directly into route handlers — so the logic is exercisable independent of any HTTP request/response cycle.

The result: the full OAuth login flow (state mismatch, successful login, session persistence, tampered-cookie rejection) is covered by automated tests that never make a real network call — only the final manual browser check against a real registered GitHub OAuth App is left untestable by automation, and that's inherent to OAuth itself (a real user consent screen has to exist somewhere), not a gap in this codebase's design.

### ⚠️ Known limitation: OAuth scope is narrowed to `public_repo`, but still broader (and narrower) than this app really wants

The GitHub OAuth scope requested (`build_authorize_url`'s `scope` param) was originally `repo` — full read/write on all public and private repositories, plus (because GitHub bundles it into that scope) the ability to manage org-owned projects, invitations, team memberships, and webhooks for any organization the user belongs to. That was dramatically more access than this service uses: per the Week 1 plan, it only ever needs to *read* PR metadata and diffs.

**Immediate fix applied:** the scope has been narrowed to `public_repo`. This removes the org-management bundling entirely (that note is specific to the `repo` scope, not `public_repo`) and limits the blast radius of what's being requested. This was a deliberate, cheap, do-it-now mitigation — not the complete fix.

**What narrowing to `public_repo` does *not* fix, and why this is still flagged as a known limitation, not closed:**

- **It's still read *and write*, not read-only.** GitHub's classic OAuth App scope model has no "read-only PRs" scope at all — `public_repo` grants write access to code, issues, and PRs on public repos, none of which this service uses, purely because no narrower option exists in this scope model.
- **It now only works for public repositories at all.** This is a real functional tradeoff, not just a security one: this service currently *cannot* review PRs on a private repo, for anyone, including its own developer. That may be an acceptable tradeoff for now, or may need revisiting depending on what repos this is meant to actually review.

**Why this still matters for the direction this project is headed:** even with the narrower scope, asking an arbitrary user (not just the developer) to authorize write access they don't need is still a real trust question the moment this is presented to anyone else — which the Month 2 plan's later weeks, and especially the MCP server work, explicitly intend to happen. `public_repo` is a reasonable stopgap for continued single-operator development, not a scope this should ship to other users behind.

**The real, complete fix, still outstanding:** migrate from a classic GitHub **OAuth App** to a **GitHub App**. GitHub Apps support fine-grained, per-permission scoping declared at registration time (e.g. "Pull requests: Read-only," on private repos too if desired, with no bundled write/webhook/org access at all), and can be installed against a chosen subset of repositories rather than implicitly covering everything the authorizing user can access. This is a different registration flow and a different token type (installation tokens rather than a plain OAuth user access token) than what Week 1 built — a real, scoped follow-up task, not a small tweak to the current OAuth flow.

---

## How to Read This Doc Over Time

Each week's section above should, by the time it's written, describe patterns that actually survived that week's work — not the plan for what a pattern would look like. If a pattern introduced in an earlier week gets revised or abandoned later (e.g. the in-memory session store eventually needs to move to Redis for a multi-instance deployment), that's worth noting in the week it actually changes, with a short pointer back to this section rather than silently rewriting history here.
