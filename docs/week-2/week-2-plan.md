# Week 2 — Agent With Tool Use

## What This Week Is

Week 1 proved this service can reliably get real data out of GitHub: PRs, diffs, retried and error-handled correctly. Nothing has looked at that data yet. Week 2 is where an LLM enters the codebase for the first time — a Claude-powered agent that decides, step by step, which tools it needs (read a file, search the codebase, check a dependency, run a linter) to actually produce a code review, rather than a single prompt-and-response call.

By the end of the week: given a real PR, hitting one endpoint kicks off a multi-step agent loop that inspects the diff, calls whatever tools it decides it needs, and returns a structured list of findings — not prose, a typed, machine-readable review.

---

## Decisions Made Up Front

- **Claude API, called directly (not via LangGraph).** Week 3 rebuilds this same agent using LangGraph specifically so the two can be compared honestly — that comparison only means something if this week's version is a real, hand-rolled agent loop, not a shortcut.
- **Four tools, matching the original scope**: `get_file_content`, `search_codebase`, `check_dependency_versions`, `run_linter`.
- **`run_linter` covers three languages this week — Python, TypeScript/JavaScript, and C#** — not scoped down to just Python. Each gets a real linter invoked as a subprocess against a single file's content, no full repo checkout required:
  - **Python** → `ruff check --stdin-filename <path> -`, reading the file over stdin. `ruff` is already a project dependency (currently dev-only, for linting this project's own code) — Week 2 adds it as a runtime dependency too, since the app itself now invokes it in production, not just during development.
  - **TypeScript/JavaScript** → [`oxlint`](https://oxc.rs/), a zero-config, single-binary linter with a PyPI wrapper (`pip install oxlint` / `uv add oxlint`) — no Node.js or `npm install` required to run it, and no project `node_modules` to resolve. Chosen over ESLint specifically because ESLint's plugin/parser resolution assumes a real project context this tool doesn't have.
  - **C#** → the one genuinely new category of complexity: there's no equivalent zero-config, single-file C# linter. The plan is to scaffold a throwaway minimal `.csproj` wrapping just the one file, then run `dotnet format analyzers --verify-no-changes` against it and parse the diagnostics — using the .NET SDK's own built-in analyzers, not a heuristic stand-in. **This requires the .NET SDK installed wherever this code runs** — a new, manual, machine-level setup step (see below), and a real infra footprint this project didn't have before (Day 5's Docker image is Python-only; it isn't updated to include Node/`.NET` this week — running the C# linter path only needs to work locally via `uv run` for now, the same "don't build the container-level need before it's real" call already made on Day 5).
  - Every language path returns the same shape (`LintFinding`: file, line, column, rule, message, severity) — the agent's tool contract doesn't change based on which linter actually ran underneath.
- **`search_codebase` uses GitHub's code search API** (`GET /search/code?q=...+repo:owner/repo`, through the installation token) rather than fetching files and grepping them in-process. It reuses the exact same auth this project already has, with no new infrastructure. The real tradeoff: GitHub's search index can lag a few seconds to minutes behind a very recent push, which matters if a review runs immediately after a push GitHub hasn't indexed yet.

  **Alternative considered, not built this week:** fetch the relevant files directly (via `get_file_content`) and search their content in-process. This avoids the indexing-lag problem and gives more control over *which* files get pulled — but that "which files" question is exactly the harder design problem. This is where the **large-diff preprocessing idea flagged back in Week 1's forward-looking notes** would actually get used: a plain-Python, no-LLM inventory pass over a PR's changed files (skip lockfiles/generated/vendored/binary files, group by file, chunk large diffs) before ever handing anything to an LLM or a search call — the same idea, now with a concrete use: it's what would make "fetch + grep locally" viable on a large PR without either blowing through the LLM's context window or reading far more of a big repo than a review actually needs. Worth building once a PR big enough to actually need it shows up in testing, not speculatively now.
- **The agent's final output is a structured findings list, not freeform text.** A Pydantic model (`ReviewFinding`: file, line, category, severity, summary — deliberately similar in shape to a typed code-review report) is what the agent must produce, via a dedicated final tool call (`submit_review`) rather than plain assistant text. This is the contract Week 3's LangGraph rebuild and Week 4's MCP server both need to produce identically — getting it typed now means neither of those weeks has to retrofit structure onto prose later.

## One Manual Step Required Before Day 3

Install the **.NET SDK** on the local dev machine (`dotnet --version` should work) — needed for the C# linter path. No account/registration involved, just a local toolchain install, closer in spirit to installing `uv` itself than to registering a GitHub App.

---

## Day-by-Day Plan

### Day 1 — Claude API wiring + `get_file_content`

- Add `anthropic` as a runtime dependency; `ANTHROPIC_API_KEY` (required) and `ANTHROPIC_MODEL` (defaulted to the current Sonnet generation) join `Settings`, following the same fail-fast pattern as every other required config value.
- `src/services/github_content.py` — `fetch_file_content(client, installation_token, owner, repo, path, ref) -> str`, via GitHub's Contents API (`GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`, base64-decoded), routed through `call_with_retry` like every other GitHub call this project makes.
- `src/schemas/review.py` — `ReviewFinding`, and the tool-facing schemas each tool's arguments/results will validate against.
- **Concept:** this is the first tool, built in isolation before there's an agent loop to call it from — proving the tool itself works independent of any LLM involvement, the same "testable by construction" discipline as every service module so far.

### Day 2 — `search_codebase` + `check_dependency_versions`

- `src/services/code_search.py` — `search_codebase(client, installation_token, owner, repo, query) -> list[CodeSearchResult]`, wrapping GitHub's code search endpoint.
- `src/services/dependency_check.py` — `check_dependency_versions(client, installation_token, owner, repo, ref)`: detects which manifest file(s) changed in the PR (`requirements.txt`/`pyproject.toml`, `package.json`, `*.csproj` — matching the same three-language scope as the linter), parses declared versions, and queries the corresponding public registry (PyPI's JSON API, the npm registry, NuGet's API) for the latest available version. Flags anything declared as a downgrade or noticeably behind latest — it doesn't attempt to resolve full dependency trees or transitive versions, just what actually changed in this diff.
- **Concept:** both tools follow the same shape as Day 1's — plain, GitHub/registry-calling functions with no LLM or FastAPI code in them, callable and testable on their own before the agent loop exists to orchestrate them.

### Day 3 — `run_linter` (Python, TS/JS, C#)

- `src/services/linters/` — a small package: `dispatch.py` (extension → linter mapping), `python_linter.py` (`ruff`), `js_ts_linter.py` (`oxlint`), `csharp_linter.py` (scaffolds the throwaway `.csproj`, runs `dotnet format analyzers`, cleans up the temp directory afterward).
- Each linter module runs its tool via `asyncio.create_subprocess_exec` (not `subprocess.run` — this project is async throughout, and a blocking subprocess call would stall the event loop), parses that tool's own output format into the shared `LintFinding` schema.
- An unsupported file extension returns a clear "unsupported language" result from the tool, not a silent no-op or an error the agent has to interpret.
- **Concept:** this is the week's most infrastructure-heavy day — three genuinely different subprocess-based tools behind one consistent interface. The .NET SDK requirement is called out explicitly here since this is the day it actually gets exercised.

### Day 4 — The agent loop

- `src/services/review_agent.py` — the orchestration itself: builds the initial message (PR diff + available tool definitions), sends it to Claude, and loops: if the response contains `tool_use` blocks, execute the corresponding tool(s), feed `tool_result`(s) back, and call Claude again; if the response is the `submit_review` tool call, validate its arguments against `ReviewFinding` and return. Capped at a fixed maximum number of iterations, so a confused agent fails loudly (a clear "exceeded max steps" error) instead of looping indefinitely and quietly burning API budget.
- `src/routers/review.py` — `POST /github-app/repos/{owner}/{repo}/pulls/{number}/review`, scoped through the same installation-auth dependency chain as every other route, running the agent loop synchronously and returning the findings list in the response. (Queueing this instead of running it synchronously is explicitly Week 3's job, once SQS enters the picture — not built here.)
- **Concept:** the actual "LLM decides which tool to call next" loop — the core mechanic every agent framework (LangGraph included) is ultimately a structured way of expressing. Building it by hand once is what makes Week 3's comparison meaningful.

### Day 5 — Tests, cost/error handling, end-to-end verification

- Unit tests for each tool in isolation (mocked GitHub/registry responses via `respx`, mocked subprocess calls for the linters).
- Tests for the agent loop itself using a mocked/fake Claude client (no real API calls in the test suite — the same "testable without hitting the real network" discipline already applied to every GitHub call), covering: a single tool call then `submit_review`, multiple sequential tool calls, and the max-iterations cap actually firing.
- Claude API failure handling: rate limits and transient errors get the same retry treatment `github_retry.py` already established for GitHub — a genuinely new consideration this week is that a failed *mid-loop* call needs to fail the whole review cleanly, not retry from message-history state that's already partially mutated.
- Manual end-to-end run against a real PR, confirming the full loop actually produces a sensible, correctly-typed findings list — not just that individual tools work in isolation.

---

## .NET parallels

- The agent loop (send messages, inspect the response for tool-call requests, execute them, feed results back, repeat) is the same shape as Semantic Kernel's or an OpenAI-Assistants-style function-calling loop in .NET — structurally identical regardless of SDK.
- Scaffolding a throwaway `.csproj` around a single file and running `dotnet format analyzers` against it is closer to a CI step than typical application code — the same idea as a build pipeline invoking `dotnet format --verify-no-changes` as a lint gate, just triggered by this service instead of a CI runner.
- A capped-iteration agent loop with a hard failure on exceeding it ≈ the same defensive instinct as a bounded retry policy (Week 1's `github_retry.py`, or Polly in .NET) — "eventually give up loudly" applied to LLM tool-calling instead of HTTP retries.

---

## Automated verification

- Each tool function tested in isolation with mocked externals (GitHub API via `respx`, registry lookups via `respx`, linter subprocesses via a mocked/stubbed process runner)
- The agent loop tested against a fake Claude client that returns a scripted sequence of tool-use/final-answer responses — no real Anthropic API calls in CI
- `run_linter`'s three language paths each get at least one test asserting a real, correctly-parsed finding from that language's actual tool output format
- The max-iterations cap is directly tested (a fake client that always requests another tool call) to confirm the loop actually terminates with a clear error, not a hang

## Manual verification

```bash
# Prerequisite: dotnet --version works locally

uv run fastapi dev src/main.py

# Against a real installed repo/PR:
curl -X POST -b cookies.txt \
  http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<number>/review

# Confirm: a structured JSON findings list comes back, each finding has a
# real file/line/category/summary, and the response reflects an actual
# multi-step tool-calling process (visible via server logs — how many
# tool calls happened, which tools were used) rather than a single
# LLM call with no tool use at all.
```

---

## End-of-week checklist

- [ ] All four tools work standalone, independent of the agent loop
- [ ] `run_linter` produces real findings for a Python, a TypeScript/JavaScript, and a C# file, each via its own real tool
- [ ] The agent loop correctly alternates between tool calls and further reasoning, and terminates via `submit_review`, not by hitting the iteration cap under normal conditions
- [ ] The iteration cap fires cleanly (a clear error, not a hang) when deliberately forced
- [ ] `POST /github-app/repos/{owner}/{repo}/pulls/{number}/review` returns a real, correctly-typed findings list against a real PR
- [ ] `uv run pytest` and `uv run ruff check .` both pass

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **A hand-rolled tool-calling agent loop before reaching for a framework** is a direct repeat of the same instinct as Week 1's hand-written retry/backoff before this project ever needed a library for it — understand the raw mechanics first, evaluate the framework's actual value second, once there's something concrete to compare it against.
- **Structured, typed final output from an LLM step, enforced via a dedicated tool call rather than parsed out of free text,** mirrors the same "typed boundaries around anything crossing into or out of this system" discipline that's applied to every external API response so far — just applied to an LLM's output for the first time instead of GitHub's.
- **A capped-iteration loop that fails loudly rather than hanging** is a new instance of the same "eventually give up, cleanly" principle `github_retry.py`'s `max_attempts` already established — the same defensive shape, now guarding against a confused agent instead of a flaky network call.
- **Installing a local toolchain (the .NET SDK) as a manual, un-automatable setup step** joins the same category as registering the GitHub App or installing `uv` itself — a recurring shape across this project: some setup lives outside anything code can do for you, and that's fine, not a gap.
