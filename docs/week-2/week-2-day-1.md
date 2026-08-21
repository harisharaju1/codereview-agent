# Day 1 — Claude API Wiring + `get_file_content`

## What was built

- **`pyproject.toml`** — `anthropic` added as a runtime dependency (not dev — this project calls Claude in production request paths starting this week).
- **`src/config/settings.py`** — `anthropic_api_key: str` (required, fail-fast) and `anthropic_model: str = "claude-sonnet-5"` (defaulted, not required).
- **`src/schemas/review.py`** — `ReviewFinding` (`file`, `line: int | None`, `category: str`, `severity: Literal["low","medium","high"]`, `summary`) and `FileContent` (`path`, `content`, `encoding`).
- **`src/services/github_content.py`** — `fetch_file_content`, the first tool: fetches and base64-decodes one file's real content from a repo at a given ref, via GitHub's Contents API, routed through the existing `call_with_retry`.
- **`src/dependencies/anthropic_client.py`** — its own `lifespan()` (an `AsyncAnthropic` client, created once, stored on `app.state`) and `get_anthropic_client()` (the `Depends()`-injectable accessor), mirroring `http_client.py`'s pattern exactly.
- **`src/main.py`** — a composed `lifespan()` that enters both `http_client.lifespan` and `anthropic_client.lifespan` in one `async with` statement.
- **Tests**: `test_github_content.py`, `test_review_schemas.py`, `test_anthropic_client.py` — 6 new tests, all mocked (no real Anthropic or GitHub calls in the suite).

## Why it's built this way

- **`ReviewFinding` and `FileContent` exist before anything produces a `ReviewFinding`.** Every prior schema in this project validated something GitHub sent back; this is the first schema this project's own logic has to satisfy. Defining it now means every day of work between now and Day 4's agent loop is written against the real, final contract — and Week 3's LangGraph rebuild and Week 4's MCP server both need to produce this exact shape, so it's worth getting right once rather than reworking it three times.
- **`anthropic_api_key` is required with no default; `anthropic_model` has one.** The distinction isn't "important vs. unimportant" — it's secret vs. plain config. A missing key means every Claude call fails, so there's no sane default and the app should refuse to boot. A missing model name has an obviously reasonable default (the current generation); forcing every environment to specify it would be pure friction.
- **The Anthropic client lives behind its own `Depends()`-injectable dependency, not constructed inline in `review_agent.py`.** This is a deliberate, small, paid-for-now cost: it's the seam that would make "let a caller supply their own API key/base URL/model" (a live product question already discussed, not being built) a one-function change later instead of a refactor of every call site that currently talks to Claude.

## Alternatives, Patterns, and Architecture Decisions

*(New section, starting this week — see "Learning Notes" in prior retrospectives for why: this project's arc — raw tool loop → LangGraph → MCP server — is meant to be understood well enough to reimplement elsewhere, and that means being explicit about the roads not taken, not just the one built.)*

**Pattern: DI-provided, lifespan-managed SDK client.** This is the second time this exact pattern has been used (`http_client.py` was the first) — a resource that's expensive or wrong to construct per-call gets created once at app startup, stored on `app.state`, and handed out via a plain `Depends()`-injectable function. The pattern generalizes to *any* SDK client with connection-pooling or session-state semantics, not just HTTP clients specifically — worth recognizing as a reusable shape, not something specific to `httpx`.

**Decision: one lifespan function per owned resource, composed in `main.py`, not one growing lifespan function.** The alternative — folding the Anthropic client's setup directly into `http_client.py`'s existing `lifespan()` — would have been less code today (no composition needed in `main.py`) but would mean `http_client.py` now owns two unrelated resources, and every future shared resource (a Redis client, say) would mean editing that same function again, making it a de facto "app startup" dumping ground. Keeping each resource's lifecycle in the module that owns it, and composing them in `main.py` via `async with a(), b():`, means adding a third resource later is a one-line addition to `main.py`, not a change to any existing dependency module. The mechanism itself — multiple async context managers entered in one `async with` statement — is just standard Python; nothing FastAPI-specific makes this work, which is worth knowing since it means this composition technique isn't tied to this framework at all.

**Alternative considered and rejected: constructing `anthropic.AsyncAnthropic()` directly inside `review_agent.py`, where it's first used.** This would have worked for Day 4's needs alone — nothing about the agent loop *requires* dependency injection to function correctly. It was rejected because the entire point of the DI seam is future flexibility that doesn't exist yet: if a caller-supplied API key ever becomes real, every place that constructs its own client would need to change; one dependency function would not. This is a real instance of a judgment call worth naming explicitly: building the seam costs a small amount of indirection today, in exchange for a change three weeks from now (if it happens at all) being small instead of invasive. That tradeoff isn't always worth making — it was made here specifically because the "bring your own model" question had already been discussed as a real, plausible product direction, not a hypothetical.

**Decision: `category` on `ReviewFinding` is a free-text `str`, while `severity` is a constrained `Literal`.** Worth stating why these two fields, both describing "what kind of finding is this," are typed so differently: `severity` has an obviously small, fixed set of sane values decided by this project itself (low/medium/high will always mean the same three things). `category` doesn't — it depends on what the eventual tools (three different linters, dependency checks, code search, and Claude's own judgment) actually surface, which isn't fully knowable yet. Constraining it now would mean guessing a taxonomy ahead of the evidence; leaving it a string defers that decision to when there's real data to base it on, at the cost of losing compile-time/validation-time guarantees about what values can appear.

## Python-specific things worth calling out

- **`async with a(), b():`** entering two async context managers in one statement is ordinary Python syntax, not something FastAPI provides — it's exactly equivalent to nesting `async with a():` then `async with b():` inside it, just flatter. Cleanup runs in reverse order automatically, the same guarantee a single context manager gives.
- **`base64.b64decode` handles embedded newlines in its input transparently** — GitHub's Contents API returns base64 content chunked with newlines every ~60 characters (presumably to keep the raw JSON's lines from being unreasonably long), and no manual stripping was needed before decoding.
- **`anthropic.AsyncAnthropic` implements `__aenter__`/`__aexit__`**, exactly like `httpx.AsyncClient` — confirmed directly before writing the lifespan function, rather than assumed, since not every async SDK client supports `async with` out of the box.

## .NET parallel

- The DI-provided, lifespan-managed Anthropic client ≈ the exact same `HttpClient`-via-`IHttpClientFactory` reasoning already applied to the shared `httpx` client, now generalized to a second SDK.
- Composing two lifespans via nested `async with` in `main.py` ≈ chaining multiple `IHostedService`/startup filters in `Program.cs`, each owning one piece of app-wide state, rather than one monolithic startup method doing everything.
- `ReviewFinding` defined well ahead of anything producing it ≈ defining a shared DTO/contract in a class library referenced by multiple projects before all of those projects exist yet — a deliberate "get the shape right once" move rather than three separate teams converging on it independently later.

## Verified manually

- `Settings()` with `ANTHROPIC_API_KEY` genuinely absent (not blank — an empty string still satisfies a required `str`, the same gotcha rediscovered on Day 5) fails fast with a clear `ValidationError`.
- `uv run pytest` (30 tests) and `uv run ruff check .` both clean.
- **Not yet verified**: a real call to the Anthropic API with a genuine key — `.env` doesn't have `ANTHROPIC_API_KEY` set yet. `.env.example` has been updated with the new fields; once a real key is added, the live smoke-test command from the Day 1 plan doc confirms the key actually works end-to-end.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **A DI-provided, lifespan-managed SDK client** is a direct, deliberate repeat of Week 1's `http_client.py` pattern — reinforced this time by writing out explicitly *why* it's structured that way (see "Alternatives, Patterns, and Architecture Decisions" above), which Week 1's docs didn't do to this level of depth.
- **Defining a typed output contract before anything produces it** is genuinely new for this project — every Week 1 schema modeled data arriving from GitHub; this is the first schema this project's own logic must satisfy, which is a different kind of design pressure (there's no external spec to validate against, only this project's own future needs).
- **The "empty string still satisfies a required `str`" gotcha reappearing** (first found on Day 5, now rediscovered testing `ANTHROPIC_API_KEY`) is worth remembering as a standing verification habit for this project specifically, not a one-off mistake: testing "fail fast on missing config" always means actually unsetting the variable, never just blanking it.
