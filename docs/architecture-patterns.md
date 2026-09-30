# Architecture & Design Patterns

A running catalogue of the structural patterns this project uses and why — updated at the end of each week's work, not each day's, so it reflects settled decisions rather than in-progress ones. Day-level reasoning and tradeoffs live in the individual `week-N-day-M.md` docs; this file is the higher-altitude view across the whole codebase.

---

## Week 1 — Python Backend Foundations & GitHub App Auth

### Layered architecture: routers → services → schemas/dependencies

The codebase is split into four kinds of modules, each with one job:

- **`routers/`** — HTTP-facing only. Parse the request, call into a service function, shape the response. A router should never contain business logic itself.
- **`services/`** — the actual logic (`github_app_auth.py`, `installation_token_cache.py`). These are plain functions/modules with no FastAPI-specific code in them at all — they could be called from a CLI script or a test with no HTTP involved.
- **`schemas/`** — Pydantic models describing the shape of data crossing a boundary (a request/response body, or an external API's response). A schema is both a type and a runtime validator at once.
- **`dependencies/`** — FastAPI-specific wiring that *provides* something to a route (the shared HTTP client, the current authenticated user) via the `Depends(...)` mechanism, sitting between routers and services.

The reason for the split: it keeps each piece independently testable and independently reasoned-about. A router change (add a new endpoint) shouldn't require touching business logic; a business-logic change (how we validate an OAuth state) shouldn't require touching HTTP-handling code.

### Dependency injection via `Depends(...)`

Nothing in this codebase constructs its own dependencies (an HTTP client, the settings object, the current user) — every route declares what it needs as a function parameter with a `Depends(...)` default, and FastAPI resolves and injects it at request time. This has two concrete payoffs already visible in Week 1:

- **Shared, reused resources.** `get_http_client` hands every route the *same* `httpx.AsyncClient` (created once at app startup), rather than each call site constructing its own — the difference between reusing a connection pool and paying for a fresh TCP+TLS handshake on every request.
- **Composable auth.** `get_current_installation_id` is "the installed app's installation, or a 401" as a single reusable dependency. Any future route that needs a valid installation (Day 3's PR/diff endpoints) just adds `Depends(get_current_installation_id)` — the check isn't re-implemented or copy-pasted per route.

### Fail-fast configuration

`Settings` (a `pydantic-settings` `BaseSettings` subclass) declares every environment variable the app needs, with no default for the ones that are genuinely required (`github_app_id`, `github_app_private_key_path`, `github_app_slug`, `session_secret_key`). Missing one of these crashes the app immediately on startup with a clear validation error, rather than the app starting successfully and failing confusingly on the first request that happens to need it.

A deliberate related discipline: a config field is only added to `Settings` in the same day's work that actually starts consuming it. The alternative — adding fields "for later" — produces required configuration that looks load-bearing but isn't, which is its own source of confusion. This discipline is also why the original OAuth App fields (`github_client_id`, `github_client_secret`, `github_oauth_redirect_uri`) were removed outright rather than left alongside the new GitHub App fields when that flow was replaced — see the note below.

### Signed opaque identifiers, not raw secrets, in cookies

The browser-facing cookie never contains a real credential. It contains an `installation_id` — meaningful, but not a secret by itself — signed (via `itsdangerous`) so tampering is detectable. The actual credentials used against GitHub's API (the App's private key, and the short-lived installation access tokens minted from it) never leave the server.

This is a general pattern, not specific to any one auth mechanism: **signing proves a value wasn't altered; it does not hide the value.** Anything genuinely sensitive belongs server-side, referenced by an opaque handle the client carries — never embedded directly in a client-readable cookie, signed or not.

### Typed boundaries around external data

Every external response this service parses (GitHub's installation-token response, installation info, repository list) is validated into a Pydantic model before anything else touches it, rather than passed around as a raw `dict`. A shape mismatch — GitHub renaming a field, or a bug constructing a mocked response in tests — fails loudly and immediately at the boundary, instead of surfacing later as a confusing `None` or `KeyError` deep in unrelated logic.

### Testability by construction, not by accident

Two choices exist specifically to make the GitHub App auth flow unit-testable without hitting the real GitHub API:

- `github_app_auth.py`'s functions take an `httpx.AsyncClient` as a parameter rather than constructing one internally — this is what lets tests substitute a mocked client (via `respx`) transparently.
- The installation-token cache is read/written through a plain function (`get_installation_token`), not baked directly into route handlers — so the caching logic (fetch-if-stale, reuse-if-fresh) is exercisable independent of any HTTP request/response cycle.

The result: the full install flow (redirect, signed-cookie callback, tampered-cookie rejection, token caching and refresh, end-to-end installation-info fetch) is covered by automated tests that never make a real network call and use a test-generated RSA keypair rather than the real private key — only the final manual browser check against the real registered GitHub App (actually clicking "Install" on GitHub's consent UI) is left untestable by automation, and that's inherent to any app-installation flow (a real user consent screen has to exist somewhere), not a gap in this codebase's design.

### Resolved: migrated from GitHub OAuth App to a GitHub App for fine-grained, installation-scoped access

Week 1 originally built GitHub login via a classic **OAuth App**, whose only available scopes were bundled and coarse: `repo` granted org-management access on top of full repo read/write; `public_repo` (the narrowed stopgap) was still read *and* write, and dropped private-repo access entirely. Neither option offered anything close to "read-only PRs on repos the owner explicitly chose."

**The fix:** the OAuth App flow (`routers/auth.py`, `services/github_oauth.py`, `services/session_store.py`, `dependencies/session.py`'s `get_current_user`, `schemas/auth.py`) was deleted outright and replaced with a **GitHub App** (`routers/github_app.py`, `services/github_app_auth.py`, `services/installation_token_cache.py`, `dependencies/installation.py`, `schemas/github_app.py`). The identity model is fundamentally different, not just a narrower scope string:

- An OAuth App impersonates a user and inherits whatever that user can access. A GitHub App **has its own identity** (authenticated via an RS256-signed JWT, minted from a private key generated at registration) and acts against an explicit **installation** — a repo owner deliberately installs the app on a specific set of repos, with specific, fine-grained, read-only-where-possible permissions (here: Pull requests, Contents, and Metadata, all read-only).
- There's no single "logged-in user" anymore. What matters is: *which installation is this request scoped to, and what can it see through that installation.* That's a better fit for a review bot than a personal-dashboard-style login, and the right shape for Week 3's PR-fetching work to build on.
- Credentials are now short-lived by design: the signed App JWT is valid ~10 minutes and is never used directly against repo data; it's exchanged for an installation access token (~1 hour) via `installation_token_cache.py`, which caches and transparently refreshes tokens close to expiry. This replaces the OAuth flow's single long-lived user token with a credential that naturally expires and is scoped to exactly one installation.

The signed-cookie *mechanism* (`itsdangerous`, sign/verify with a salt) survived the rewrite unchanged — only what it signs changed, from an opaque session id to an installation id. See the Day 2 GitHub App retrospective for the full before/after and manual verification notes.

### ⚠️ Known gap: `/github-app/callback` doesn't handle `setup_action=request`

GitHub's install redirect sends `setup_action` as one of three values: `install`, `update`, or `request`. The third happens when the App requires org-admin approval and a non-admin org member requests an install instead of completing one — critically, **no `installation_id` is sent in that case**, since nothing was actually installed.

`callback` in `src/routers/github_app.py` declares `installation_id: int` as a required query param, so a `request` redirect would currently fail FastAPI's own validation with a `422` rather than being handled gracefully (e.g. acknowledging the pending request without trying to sign/cache an installation id that doesn't exist).

This can't happen yet: `setup_action=request` is only reachable once the App's install target is something other than "Only on this account" (org installs with an approval requirement, or "Any account"). Not fixing now, since there's no way to exercise or verify the fix until that setting actually changes — revisit this note if/when the App is opened up to org installs or "Any account."

---

## Week 2 — Tool-Use Agent Loop & Claude Integration

### Agent-visible tool contract ≠ underlying Python function signature

Every tool the agent can call (`get_file_content`, `search_codebase`, `check_dependency_versions`, `run_linter`) has two layers: a hand-written JSON Schema `input_schema` describing what Claude is allowed to supply, and a small **executor function** that bridges those narrower arguments to the real service function underneath, closing over everything the model shouldn't see or control (the `httpx.AsyncClient`, the installation token, `owner`/`repo`, and — deliberately, after reconsidering the original plan mid-build — the PR's fixed head ref). `run_linter`'s agent-visible shape is just `{"path": string}`; the executor is what actually fetches the file's content and hands it to the real `run_linter(path, content)`. The model never sees a credential, and never supplies an argument (like a ref) that has no legitimate second value to ever be.

This is the same instinct as a controller action's DTO being narrower than the domain service method it calls — applied here to an LLM's view of a function instead of an HTTP caller's.

### The tool-call loop: send → inspect → execute → append → resend

`run_review_agent` (`services/review_agent.py`) is a hand-rolled implementation of the mechanic every LLM tool-calling API works the same way underneath: send the conversation plus tool definitions, inspect the response for `tool_use` requests, execute them locally, append both the model's request and the tool results back into the conversation, and resend — until the model calls a dedicated `submit_review` tool (not free text) with output validated against a real Pydantic model, or a hard iteration cap is hit. Built by hand deliberately, not via a framework, specifically so Week 3's LangGraph rebuild has a real baseline to compare against — see `docs/week-2/week-2-day-4-plan.md`'s background section for the mechanics in full.

A validation error on `submit_review`'s input is fed back to the model as a recoverable error, not a hard failure — the same "let it self-correct within a bounded budget" idea `github_retry.py` already established for a flaky network call, now applied to a model producing malformed structured output.

### Structured output via a dedicated tool call, not parsed free text

`ReviewFinding` (the agent's final output) is validated directly against `tool_use.input` for a `submit_review` call, never extracted from the model's prose. This is the same "typed boundaries around external data" principle Week 1 applied to every GitHub response, now applied to an LLM's output for the first time — and it's more reliable in practice, too: a schema presented as a tool's `input_schema` gets followed far more consistently than an instruction to "reply in this JSON format" as plain text.

### Cost is tracked per run, attached to both success and failure

`ReviewResult` (success) and both agent-specific exceptions (`AgentDidNotSubmitReviewError`, `AgentExceededMaxIterationsError`) all carry a `ReviewUsage` — token counts summed across every loop iteration, plus an estimated USD cost from a small, isolated pricing table (`services/model_pricing.py`). Attaching usage to the exceptions too, not just the success path, was a fix made after a real failure: a review that exhausts its iteration budget has still spent real, billed tokens getting there, and that cost was invisible anywhere in the response until this was corrected. The pricing table matches by prefix, not exact equality, because Anthropic's real API responses echo back a dated snapshot suffix (`claude-haiku-4-5-20251001`) the configured model string never has — found only by testing against the real API, not from the mocked test suite, which is worth remembering as a category of gap mocks structurally cannot catch.

### ⚠️ Known limitation: the agent's actual behavior is coupled to which model is configured

`MAX_ITERATIONS` (8) and the system prompt's lack of any budget-awareness were tuned against no specific model or PR size. Testing against a real, sizeable PR with `claude-haiku-4-5` (chosen deliberately for cost during this testing phase) surfaced a real gap: Haiku calls exactly one tool per turn rather than batching several into one turn, burning through the iteration budget faster than a model with stronger planning would on the same PR — not a bug in the loop's mechanics, but a real behavioral coupling between "which model is configured" and "how large a PR this loop can actually finish reviewing." Not fixed yet; real options (raising the cap, adding explicit budget-awareness to the prompt, forcing `tool_choice` toward `submit_review` on the final iteration) are recorded in `docs/week-2/week-2-day-5.md`.

### First instance of logging in this project

`services/review_agent.py` is the first place this project logs anything — added specifically because an agent loop's actual path (which tools, how many times, why it stopped) is decided by the model at runtime and isn't reproducible from the request alone, unlike every other deterministic code path built so far. `logging.basicConfig(...)` lives once, in `main.py`, since nothing else configures it and Python's default logging behavior silently drops anything below `WARNING`. This is deliberately not a project-wide observability setup — just the first place the tradeoff was judged worth its cost.

---

## How to Read This Doc Over Time

Each week's section above should, by the time it's written, describe patterns that actually survived that week's work — not the plan for what a pattern would look like. If a pattern introduced in an earlier week gets revised or abandoned later (e.g. the in-memory session store eventually needs to move to Redis for a multi-instance deployment), that's worth noting in the week it actually changes, with a short pointer back to this section rather than silently rewriting history here.
