# Day 1 Plan — Claude API Wiring + `get_file_content`

## What we're doing and why

This is the first day an LLM enters this codebase at all. Before there's any agent loop to orchestrate tool calls, two things need to exist independently: a way to talk to Claude (configured, testable, and not hardcoded), and the first tool the eventual agent will call — `get_file_content`, which fetches a single file's real content from a repo at a given ref. Building the tool before the loop that calls it continues the same discipline Week 1 used for every GitHub-calling function: prove the plain function works on its own, with no framework or LLM involvement, before wiring it into something more complex.

Nothing this project has done so far has needed a schema for *output the app itself produces* — every schema until now has validated something GitHub sent back. Today also introduces `ReviewFinding`, the shape the whole agent loop is ultimately building toward, even though nothing produces one yet.

---

## File 1 — `pyproject.toml` — add the `anthropic` dependency

Added to `dependencies` (runtime), not `dependency-groups.dev` — this project calls Claude in production request paths starting this week, the same reasoning `httpx` and `pyjwt` were added as runtime dependencies rather than dev ones.

---

## File 2 — `src/config/settings.py` additions

```python
anthropic_api_key: str
anthropic_model: str = "claude-sonnet-5"
```

`anthropic_api_key` is required, no default — missing it crashes the app at startup with a clear error, the same fail-fast pattern every required credential in this project has followed since Day 1. `anthropic_model` gets a real default rather than being required, since a sensible default model exists and forcing every environment to specify it adds friction without adding safety — this mirrors `environment`'s `Literal` default from Day 1, not the required-secret fields.

---

## File 3 — `src/schemas/review.py` — new Pydantic models

- `ReviewFinding`: `file: str`, `line: int | None`, `category: str`, `severity: Literal["low", "medium", "high"]`, `summary: str` — `line` is optional because not every finding is line-specific (a missing dependency-version check, for instance, applies to a whole file or the PR generally).
- `FileContent`: `path: str`, `content: str`, `encoding: str` — the validated shape of what `get_file_content` returns, rather than a bare string, so later tools/tests have something to assert against beyond "a string came back."

**Why `ReviewFinding` is defined now, days before anything produces one:** Day 4's agent loop needs `submit_review`'s tool schema to already exist as a concrete Pydantic model — defining it here means every day between now and then that touches review output is working against the same real contract, not a placeholder that gets swapped out later.

---

## File 4 — `src/services/github_content.py` — the first tool

- `async def fetch_file_content(client: httpx.AsyncClient, installation_token: str, owner: str, repo: str, path: str, ref: str) -> FileContent` — `GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`, authenticated with the installation token, routed through `call_with_retry` like every other GitHub call in this project. GitHub's Contents API returns file content base64-encoded; this function decodes it before returning, so callers never handle GitHub's transport encoding directly.

**Why this takes `installation_token: str`, not `installation_id: int`:** matches the exact pattern `pull_requests.py`'s service functions already established — the token-resolution step (checking the cache, minting if stale) happens once, at the call site, not inside every function that needs a token.

---

## File 5 — `src/dependencies/anthropic_client.py` — the injectable Claude client

- A lifespan-managed `anthropic.AsyncAnthropic` client (mirroring `dependencies/http_client.py`'s pattern exactly: created once at app startup, stored on `app.state`, provided via a `Depends()`-injectable function), rather than constructed fresh per call or hardcoded inline wherever it's used.

**Why this matters beyond just following the existing pattern:** building the Claude client behind a `Depends()` seam now — instead of importing `anthropic` and constructing a client directly inside `review_agent.py` later — is exactly the seam that would make a future "let the caller supply their own API key/model" change (discussed as a live product question, not built now) a swap of what this dependency returns, rather than a change to the agent loop itself.

---

## .NET parallels

- The `anthropic` SDK's async client, lifespan-managed and DI-provided ≈ the exact same reasoning already applied to `httpx.AsyncClient` — a `HttpClient`-via-`IHttpClientFactory`-shaped problem, just for a different SDK.
- `ReviewFinding` as a Pydantic model defined well before anything produces one ≈ defining a DTO/contract interface early in a .NET codebase specifically so multiple components (here: Day 4's agent loop, eventually Week 3's LangGraph rebuild, Week 4's MCP server) can all be written against the same typed shape without waiting on each other.
- Base64-decoding GitHub's Contents API response inside the service function, not at the call site ≈ keeping a transport-level decoding detail inside the class/method that owns the HTTP call, not leaking it out to callers — the same encapsulation instinct either ecosystem rewards.

---

## Automated verification (no real Anthropic or GitHub credentials needed)

- `fetch_file_content`, mocked via `respx`: a successful call returns a correctly base64-decoded `FileContent`; a `404` (file/ref doesn't exist) surfaces as an `httpx.HTTPStatusError`, consistent with how every other not-found case in this project currently behaves (translation into a service-level 404 happens at the router layer, same as Day 3 — not built here, since there's no route calling this yet)
- `ReviewFinding`/`FileContent` validate correctly-shaped input and reject malformed input (missing required fields, an invalid `severity` value)
- The `anthropic_client` dependency resolves to the same client instance across multiple calls within one app lifecycle, the same assertion already proven for `get_http_client`

## Manual verification

```bash
# Confirms Settings actually requires the new fields
ANTHROPIC_API_KEY= uv run python -c "from src.config.settings import Settings; Settings()"
# Expect: a clear ValidationError, not a silent default

# Confirms the real Anthropic key works at all, independent of anything
# this project builds on top of it
uv run python -c "
import asyncio
from src.config.settings import get_settings
import anthropic

async def main():
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=32,
        messages=[{'role': 'user', 'content': 'Reply with exactly: ok'}],
    )
    print(response.content)

asyncio.run(main())
"
```

---

## End-of-day checklist

- [ ] `ANTHROPIC_API_KEY` missing → app fails to start with a clear error
- [ ] `fetch_file_content` returns real, correctly decoded file content against a real repo
- [ ] `ReviewFinding` and `FileContent` reject invalid input in tests, not just accept valid input
- [ ] The Anthropic client dependency is confirmed shared (not reconstructed) across requests, same as `get_http_client`
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **A DI-provided, lifespan-managed SDK client** is a direct repeat of the exact pattern `dependencies/http_client.py` established in Week 1 — the same shape now applied to `anthropic.AsyncAnthropic` instead of `httpx.AsyncClient`, reinforcing that this is a settled house pattern for "any client that should be created once and shared," not something specific to HTTP.
- **Defining a typed output contract (`ReviewFinding`) well before anything produces it** is a new instinct for this project specifically — every schema so far modeled something external (GitHub's responses); this is the first schema this project's own logic is responsible for satisfying, not just validating against.
- **Building the first tool in isolation before the agent loop exists** repeats the same "prove the plain function works before wiring it into something bigger" discipline Week 1 used for every GitHub-calling service function — now applied to a tool an LLM will eventually call, rather than a route handler.
