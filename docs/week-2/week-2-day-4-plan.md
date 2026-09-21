# Day 4 Plan — The Agent Loop

## Why this day matters more than the others

Every day so far this week built one independent piece: a tool that fetches a file, a tool that searches code, a tool that checks dependencies, a tool that lints. None of them decide anything — they're just functions that do exactly what they're called to do, in a fixed order chosen by whoever calls them. Today is where that changes. Today, an LLM is handed the four tools and the actual decision-making: *which* tool to call, with *what* arguments, *how many times*, in *what order*, based on what it's already learned from earlier tool results — until it decides it has enough information to produce a review.

This is also the day this project's whole Month 2 arc pivots on. The plan (`docs/learning-plan-final-August.md`'s Month 2 section, not referenced again after this) calls for building this same capability three times: by hand this week, via LangGraph next week, and exposed as an MCP server the week after. The comparison between "hand-rolled" and "LangGraph" only means something if what's built today is a *real* loop — not a shortcut, not a single prompt-and-parse call dressed up to look like one. Understanding today's mechanics precisely is what makes next week's LangGraph version legible as "the same thing, done with a framework's help" rather than an unrelated new topic.

---

## Background: what "tool use" actually means, mechanically

This section exists because the rest of the plan assumes it. If any of this is unclear, the official reference is Anthropic's own documentation at **docs.claude.com**, under the "tool use" / "agents and tools" section — worth reading directly rather than trusting a paraphrase for anything load-bearing. What follows is this project's own working understanding, confirmed directly against the installed `anthropic` Python SDK (version 1.0.0) before writing this plan, not assumed from memory.

**The core idea:** normally, calling an LLM means "send text, get text back." Tool use adds a third option to what the model can send back: instead of (or alongside) a text reply, the model can send back a *structured request to call a function you told it about* — a tool name and a set of arguments, shaped according to a JSON schema you provided. The model never actually calls anything itself. It has no code execution, no network access. It only ever says, in effect, "if you don't mind, please call `get_file_content` with these arguments, and tell me what comes back." Your code is what actually runs the function, and your code decides whether to send the result back and let the conversation continue.

**Concretely, one request to Claude's Messages API (`client.messages.create(...)`) looks like this:**
- `model`: which model to use (`Settings.anthropic_model`)
- `messages`: the conversation so far — a list of `{"role": "user" | "assistant", "content": ...}` entries
- `tools`: a list of tool definitions, each `{"name": ..., "description": ..., "input_schema": {...JSON Schema...}}` — this is literally how the model learns what tools exist and what arguments they take. There's no separate registration step; the tools list is just part of every request.
- `system` (optional): instructions that apply to the whole conversation, not tied to any one message — this is where "you are reviewing a PR, here's the diff, here's how to use your tools" instructions belong, kept separate from the PR diff itself (which goes in the first `user` message).

**The response** (an `anthropic.types.Message` — confirmed via `Message.model_fields`) has, among other fields:
- `content`: a list of content *blocks*. Each block has a `type`. The two that matter here:
  - `TextBlock` (`type: "text"`, `text: str`) — the model just talking.
  - `ToolUseBlock` (`type: "tool_use"`, `id: str`, `name: str`, `input: dict`) — a request to call a specific tool with specific arguments. `id` is important: it's what ties this specific tool call to its eventual result in the next message.
- `stop_reason`: why the model stopped generating. The value that matters for this loop is `"tool_use"` — it means "I've requested at least one tool call, and I'm waiting for the result(s) before continuing." Any other stop reason (most commonly `"end_turn"`) means the model is done talking for this turn without requesting a tool call.

**The loop, in full:**
1. Send the initial request (system prompt + PR diff as the first user message + the four tool definitions + the `submit_review` tool definition, see below).
2. If `stop_reason == "tool_use"`: for every `ToolUseBlock` in the response's `content`, actually run the corresponding Python function with the arguments the model provided, and collect each result.
3. Append the model's own response (`response.content`, the raw list of blocks — including any `ToolUseBlock`s) to `messages` as a new `{"role": "assistant", ...}` entry. This matters: the model needs to see its own prior tool-call requests in the conversation history, or the next response won't make sense in context.
4. Append a new `{"role": "user", "content": [...]}` entry whose content is a list of `tool_result` blocks — one per `ToolUseBlock` from step 2, each shaped `{"type": "tool_result", "tool_use_id": <the matching ToolUseBlock's id>, "content": <the tool's result, as a string>}` (confirmed via `ToolResultBlockParam`'s fields). This is, mechanically, how "here's what happened when I ran that" gets back to the model — tool results are just a specially-shaped *user* message, not some separate channel.
5. Send the request again with the updated `messages`. Go back to step 2.
6. Eventually, the model either stops requesting tools (`stop_reason != "tool_use"`) or — in this project's design — requests the special `submit_review` tool, which isn't a real data-fetching tool at all; it's how the model hands back its final, structured answer instead of free text. Seeing a `submit_review` call is what ends the loop successfully.

**Why a dedicated `submit_review` tool, instead of just reading the model's final text reply:** a `ToolUseBlock`'s `input` is validated against the JSON schema you gave it — the model is far more reliable at producing something schema-conformant when the schema is presented as a tool's `input_schema` than when it's asked to "reply in this JSON format" as plain text (which regularly gets wrapped in markdown fences, prefixed with commentary, or subtly malformed). Treating "submit the review" as just another tool call is what lets `ReviewFinding` validation (Pydantic, exactly like every other typed boundary in this project) apply directly to `tool_use.input`, with no text-parsing step in between at all.

---

## New GitHub call needed: fetching a PR's head SHA

None of Week 2's tools so far need to know *which exact commit* a file should be read at — `fetch_pull_request_diff` (Day 3, Week 1) just wants a PR number. But `get_file_content` and `run_linter` both need a `ref` (a commit SHA or branch name) to fetch a file at a specific point in the repo's history — and the correct ref for a review is the PR's **head** commit (the latest commit on the PR's branch), not `main`/`master`.

- **`src/schemas/pull_request.py` addition**: `PullRequestDetail` — `number: int`, `title: str`, `head_sha: str` (mapped from GitHub's nested `head.sha` field).
- **`src/services/pull_requests.py` addition**: `fetch_pull_request(client, installation_token, owner, repo, number) -> PullRequestDetail` — `GET /repos/{owner}/{repo}/pulls/{number}` with the default JSON `Accept` header (unlike `fetch_pull_request_diff`, which deliberately requests the diff media type instead — these are the same URL, two different `Accept` headers, two different response shapes, exactly the content-negotiation pattern already used elsewhere in this project).

---

## The agent-facing tool contract is NOT the same as each tool's Python function signature

This is a design decision worth stating explicitly, because it's easy to assume "the tool the agent calls" and "the Python function that does the work" are the same thing with the same parameters. They aren't, and shouldn't be:

- `get_file_content`'s real Python signature takes `client`, `installation_token`, `owner`, `repo`, `path`, `ref` — six parameters. The model should never see or supply `client`, `installation_token`, `owner`, or `repo`: those are fixed for the whole review (this endpoint is already scoped to one specific `owner/repo`, authenticated via the installation token resolved once at the start), and `installation_token` in particular is a credential that has no business being something an LLM even sees, let alone controls. The **agent-facing** tool `get_file_content` therefore only exposes `{"path": string, "ref": string}` as its `input_schema` — everything else gets closed over by the executor function at the point it's registered, before the loop ever starts.
- `run_linter`'s real Python signature takes `path` and `content` — but `content` would mean asking the model to somehow supply an entire file's text as a JSON argument, which is wasteful, error-prone, and defeats the purpose of having a `get_file_content` tool in the first place (the model would need to already have the content to send it, at which point it could just read it directly instead of asking a linter to). The agent-facing `run_linter` tool exposes only `{"path": string}` — the *executor function* (not the model) is responsible for calling `fetch_file_content` first, internally, then feeding the result into the real `run_linter(path, content)`. The model asks "lint this path"; it never handles the file's actual bytes for this particular tool.

**Pattern:** every agent-facing tool is `(agent-visible arguments) -> result`, and each one is backed by a small **executor function** that closes over everything the model shouldn't see or doesn't need to supply, then calls into Week 2's already-built service functions. This executor layer is new code written today; the service functions underneath it (`fetch_file_content`, `search_codebase`, `check_dependency_versions`, `run_linter`) are unchanged from Days 1–3.

---

## File 1 — `src/schemas/pull_request.py` addition

`PullRequestDetail` (see above).

## File 2 — `src/services/pull_requests.py` addition

`fetch_pull_request` (see above).

## File 3 — `src/services/review_agent.py` — the loop itself

- A module-level list of **tool definitions** (the `{"name", "description", "input_schema"}` dicts sent to Claude) for all five tools (`get_file_content`, `search_codebase`, `check_dependency_versions`, `run_linter`, `submit_review`). `input_schema` for each is a plain JSON Schema dict — hand-written rather than derived from a Pydantic model's `.model_json_schema()`, because the agent-facing shape is deliberately narrower than any existing Pydantic model (see above), so there's no single existing model whose schema would actually match what should be exposed.
- A module-level dict mapping tool name → **executor function**, each an `async def executor(client, installation_token, owner, repo, ref, arguments: dict) -> str` — every executor's job is: pull the model-supplied arguments out of `arguments`, call the real Week 1–3 service function(s) with everything else closed over from the outer call, and return a **string** (tool results are always strings or content blocks per `ToolResultBlockParam` — a Pydantic result gets `.model_dump_json()`'d before being handed back).
- `async def run_review_agent(client, anthropic_client, settings, installation_token, owner, repo, pr_number) -> list[ReviewFinding]`:
  1. `fetch_pull_request(...)` for the head SHA, `fetch_pull_request_diff(...)` for the diff text.
  2. Build the initial `messages` list: one `user` message containing the diff and PR metadata. Build the `system` prompt: reviewer instructions, plus an explicit instruction to call `submit_review` exactly once, when done, with its findings.
  3. Loop (capped at `MAX_ITERATIONS`, a module constant): call `anthropic_client.messages.create(...)`; if `stop_reason != "tool_use"`, treat this as the agent finishing without submitting a review — a real, named error condition (`AgentDidNotSubmitReviewError` or similar), not a silent empty return. If any `ToolUseBlock.name == "submit_review"`, attempt to validate its `input` against a `SubmitReviewArgs` Pydantic model (`findings: list[ReviewFinding]`); on success, return `findings` and stop. On a `ValidationError`, **don't fail the whole review** — feed the validation error back as a `tool_result` with `is_error=True` and let the model try again (bounded by the same iteration cap as everything else). This is the same "let the agent self-correct within a bounded budget" idea the rate-limit/backoff logic already uses for a different kind of failure (Week 1's `github_retry.py`), applied here to a model producing malformed structured output instead of a flaky network call.
  4. For every other `ToolUseBlock`, look up its executor, run it, collect the string result (or, on the executor raising, a `tool_result` with `is_error=True` and the exception's message — a genuinely failed tool call should be visible to the model as a failure, not silently dropped or treated as an empty success).
  5. Append the assistant message and the tool-result user message (see the loop mechanics above), and continue.
  6. Exceeding `MAX_ITERATIONS` without a validated `submit_review` call raises a clear error — the same "eventually give up loudly, not silently" principle as `github_retry.py`'s `max_attempts`.

## File 4 — `src/routers/review.py`

- `POST /github-app/repos/{owner}/{repo}/pulls/{number}/review` — depends on `get_current_installation_id`, `get_settings`, `get_http_client`, `get_anthropic_client`; resolves the installation token via the existing cache; calls `run_review_agent`; returns the `list[ReviewFinding]` as JSON. Errors from the agent loop (exceeded iterations, no submit_review) surface as a `502`-equivalent — the review genuinely failed to produce a result, which is a different situation from a GitHub `404` (Day 3's translation logic is untouched; this is new, agent-specific error handling, not a variant of it).
- Deliberately **synchronous** — the request blocks until the review completes. Queueing this behind SQS is explicitly Week 3's job once that infrastructure exists; building it now would be solving a problem (many concurrent long-running reviews) this project doesn't have yet.

---

## .NET parallels

- The tool-call loop itself (send messages, inspect the response for function-call requests, execute them, append results, resend) is structurally identical to Semantic Kernel's or Azure OpenAI's function-calling loop in .NET — the *shape* (a `while` loop keyed on "did the model ask for more tools") isn't Anthropic-specific, it's how every major LLM tool-calling API works once you strip away each SDK's own convenience wrappers.
- Feeding a `ValidationError` back into the loop as an `is_error` tool result, so the model can self-correct, is the same idea as a resilient `IValidatableObject`-backed API returning a structured 400 that a *retrying client* could act on — except here, the "retrying client" is the model itself, inside the same request/response cycle, not a separate outer retry policy.
- The executor-function layer (agent-facing tool ≠ underlying service function) ≈ an API controller action's DTO being intentionally narrower than the domain service method it calls — the controller doesn't expose every constructor parameter of the underlying service, just what the caller should actually be able to supply.

---

## Automated verification (no real Anthropic API calls)

- A **fake Anthropic client** — not a `respx`-mocked HTTP call (Anthropic's SDK doesn't go through `httpx` the way this project's other dependencies do; it depends on its own `httpx2` client internally, confirmed while checking the SDK's shapes for this plan). The fake implements just enough of `AsyncAnthropic`'s interface (`.messages.create(...)`) to return a pre-scripted sequence of `Message`-shaped objects, so `run_review_agent` can be tested without any real network call or API key.
- Tests: a single tool call then `submit_review` with valid findings (the common case); multiple sequential tool calls before `submit_review`; a `submit_review` call with invalid `input` followed by a corrected one (proving the self-correction path actually works); the `MAX_ITERATIONS` cap firing when the fake client always requests another tool call and never submits; a tool executor raising an exception, confirmed to surface as an `is_error` tool result rather than crashing the whole loop; and each of the four real executors tested in isolation (mocked service-layer calls, the same `respx`/subprocess-mocking approach already used in Days 1–3, just exercised through the executor's narrower agent-facing argument shape).

## Manual verification

```bash
uv run fastapi dev src/main.py

curl -X POST -b cookies.txt \
  http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<number>/review

# Confirm: a real, structured JSON findings list comes back. Check server
# logs for evidence of an actual multi-step process (which tools were
# called, how many iterations it took) rather than a single LLM call with
# no tool use at all.
```

---

## End-of-day checklist

- [ ] `fetch_pull_request` returns a real head SHA against a real PR
- [ ] The agent loop correctly alternates between tool calls and further reasoning, for at least one real PR where that's genuinely necessary (not just a trivial one-tool-call case)
- [ ] A validation error on `submit_review`'s input is fed back to the model and recoverable, not an immediate hard failure
- [ ] The iteration cap fires cleanly against a fake client that never submits
- [ ] A failing tool executor surfaces as a visible `is_error` tool result, not a crash
- [ ] `POST /github-app/repos/{owner}/{repo}/pulls/{number}/review` returns a real, correctly typed findings list against a real PR
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **The tool-call loop's shape (send → inspect → execute → append → resend) is genuinely new to this project**, but the *reasoning style* behind several of its details isn't: feeding a validation error back for self-correction mirrors `github_retry.py`'s bounded-retry-then-give-up-loudly principle; the executor layer narrowing what's exposed mirrors the DTO-narrower-than-service-method instinct already present in this project's router/service split from Week 1.
- **This is the first LLM-driven control flow in either this project or the earlier TypeScript project** — everything before this point (in both projects) was deterministic: given the same input, the same code path runs every time. An agent loop's actual path through the code (how many tool calls, which ones, in what order) is decided at runtime by the model, not by this project's own logic — a genuinely different category of thing to test and reason about, which is exactly why the fake-client testing approach above exists: correctness here means "the loop mechanics are right for any sequence of model decisions," not "this one specific path is right."
