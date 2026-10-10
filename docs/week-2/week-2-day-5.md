# Day 5 — Manual Verification, Cost Tracking, and Logging

No plan doc for this day — same as Days 2 and 3, this was built directly and documented after, since the actual scope only became clear once manual testing surfaced real gaps rather than ones anticipated up front.

## What happened

With a working `ANTHROPIC_API_KEY` finally in place (the original key was found revoked earlier in the week), the first real end-to-end test was run: a `POST /github-app/repos/.../pulls/1/review` against PR #1 — the `raw-agent-loop` branch's own PR, containing the entire Day 4 build (11 files, ~1,179 insertions).

It failed: `AgentExceededMaxIterationsError`, after 8 iterations, having never called `submit_review`. Two real gaps surfaced from this single failed run, both fixed the same day.

## What was built

- **Logging** (`src/services/review_agent.py`, `src/main.py`) — the first logging anywhere in this project. `logging.basicConfig(...)` in `main.py` (nothing else in the process configures it, and without it `logger.info(...)` calls are silently swallowed by Python's default "warnings and above only" behavior). Per-iteration, per-tool-call, and per-outcome log lines in the loop, at `INFO` for normal activity and `ERROR`/`WARNING` for failures.
- **Cost tracking** (`src/schemas/review.py`, `src/services/model_pricing.py`, `src/services/review_agent.py`, `src/routers/review.py`) — `ReviewUsage`/`ReviewResult`, a small isolated pricing table with prefix-matching for Anthropic's dated model snapshot IDs, and usage summed across every loop iteration. Both failure exceptions now carry `usage` too, and the router surfaces it in the `502` response's `detail`, not just in a successful `200`.
- **A second test PR** (`test/small-readme-pr`, branched off `main`) — a genuinely small, one-file change, specifically so the `/review` endpoint has a realistic target to actually complete against, rather than only ever the large `raw-agent-loop` PR.

## Why: diagnosing the `MAX_ITERATIONS` failure

Once logging existed, re-reading the captured log line-by-line showed the loop behaving correctly, just not converging in time. Three compounding, real causes — not a bug in the loop's mechanics:

1. **The PR itself is genuinely large.** Reviewing 11 files thoroughly is real work; needing more than 8 tool calls to do that isn't the model being confused.
2. **One tool call per turn, never batched.** The log shows exactly one `tool call` between every `iteration N/8` line — never two or three in the same turn, even though the loop already supports parallel tool use (multiple `tool_use` blocks in one turn, handled correctly since Day 4). The model simply never chose to batch.
3. **Haiku, chosen deliberately for cost during this testing phase, is the likely cause of #2.** It's Anthropic's smallest/fastest tier, and noticeably weaker than Sonnet/Opus at planning ahead and batching related tool calls on a genuinely multi-file task. This is a real, named cost of that choice, not a defect — worth knowing, not necessarily worth reversing.
4. **The system prompt gives the model no budget awareness** — nothing tells it how many calls remain or nudges it to prioritize/batch as the budget tightens.

`MAX_ITERATIONS = 8` wasn't wrong on Day 4; it was tuned against a hypothetical PR, not the specific large one it first got tested against.

## Alternatives, Patterns, and Architecture Decisions

**Decision: usage lives on the exception, not in a second parallel response schema for failures.** The alternative — some kind of `FailedReviewResult` shape returned instead of raised — was rejected because this project's existing failure modes are all raised exceptions (`AgentDidNotSubmitReviewError`, GitHub's `_translate_github_error`), and inventing a "sometimes I return a result-shaped object even on failure" convention would be a new, inconsistent pattern for this one case. Attaching `usage: ReviewUsage` directly to the exception and having the router read `exc.usage` when building the `HTTPException.detail` keeps every failure mode raised the same way; only what the router does with the caught exception differs.

**Decision: a closure (`_current_usage()`), not a free function, for building `ReviewUsage` at three different exit points.** It needs `total_input_tokens`, `total_output_tokens`, and `settings` — all local to `run_review_agent`'s own scope. A free function would need all three passed in explicitly at each of the three call sites; the closure reads them directly, and there's exactly one place responsible for "how usage becomes a `ReviewUsage`," used identically whether the loop succeeds, gives up early, or exhausts its budget.

**Decision: `model_pricing.py`'s cost lookup matches by prefix, not exact string equality — discovered as necessary, not designed in speculatively.** Requesting `"claude-haiku-4-5"` gets back `response.model == "claude-haiku-4-5-20251001"` from the real API (confirmed directly, not assumed) — a dated snapshot suffix the configured model string never has. Exact matching would have silently priced every real response as "unknown," making `estimated_cost_usd` always `None` in production despite working in every test (since the fake test client never had to reproduce Anthropic's actual snapshot-suffix behavior). This is a concrete instance of a category of bug that's easy to miss entirely without ever running against the real API.

**Not fixed, deliberately, and worth naming:** the root cause (Haiku not batching, no budget-aware prompting) wasn't addressed this day — only its symptom (an invisible cost on failure) was. Real options for the root cause, left for later: raise `MAX_ITERATIONS`, add explicit budget-awareness to `SYSTEM_PROMPT` ("you have N tool calls remaining, prioritize and batch related checks"), or build the "force `tool_choice` to `submit_review` on the final iteration" idea already flagged as considered-but-not-built in Day 4's own retrospective — which would now also double as a way to guarantee a best-effort review even when the model doesn't converge cleanly.

## Python-specific things worth calling out

- **`logging.basicConfig()` must be called exactly once, at import time, before any logger is used** — calling it later, or more than once, doesn't reliably reconfigure already-created loggers. Placing it at the top of `main.py`, before the app or any router is even imported in a way that would trigger logging, is what guarantees every `logger.info(...)` call anywhere in the codebase actually reaches the console.
- **`str.startswith()` prefix matching, sorted longest-first**, is a simple, dependency-free way to handle "one string is a versioned/dated variant of another" — no regex or semver library needed for this specific case, since Anthropic's snapshot suffix format (`-YYYYMMDD`) is always appended, never inserted or prefixed.

## .NET parallel

- `logging.basicConfig()` configuring the whole process's log output once at startup ≈ configuring Serilog/`ILoggerFactory` once in `Program.cs` — both are "set this up before anything logs, or output silently goes nowhere" patterns.
- Attaching diagnostic context (`usage`) directly to a custom exception type ≈ a custom .NET exception carrying structured `Data` or additional properties beyond `Message` — the same instinct of "the exception itself should carry what a caller needs to react intelligently," not just a string.

## Verified manually

- Logging confirmed working: a real run's terminal output showed per-iteration and per-tool-call lines, which is what made the `MAX_ITERATIONS` diagnosis in this doc possible at all.
- The prefix-matching fix in `model_pricing.py` was confirmed against the real API's actual response shape (`claude-haiku-4-5-20251001`), not just the test suite's fakes.
- `uv run pytest` (61 tests) and `uv run ruff check .` both clean.
- **Still not verified**: a full successful end-to-end run producing real `ReviewFinding`s. The small test PR (`test/small-readme-pr`) exists specifically to make that verification actually achievable — running it against that PR, instead of the much larger `raw-agent-loop` PR, is the next concrete manual step.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **A real bug (the prefix-matching gap) that only a genuine external-API test could have caught** repeats the same lesson Day 3's `dotnet format analyzers` discovery taught — mocked tests prove internal logic is self-consistent, not that it agrees with what a real external system actually does. Two separate, real instances of this now, both worth remembering as a standing reason to occasionally test against the real thing, not just mocks.
- **Adding logging only once a real failure was genuinely hard to diagnose without it** — rather than building an observability layer speculatively — matches this project's consistent "build the need when it's real" discipline (Docker's deferred deploy, the linters' deferred container support). Logging is the first case of that discipline applying to *code quality/debuggability* rather than infrastructure.
- **The batching-behavior gap (Haiku not parallelizing tool calls) is a genuinely new kind of finding for this project** — the first time a specific model's own capability limits, not this project's code, were the direct cause of a failure. Worth remembering as its own category distinct from "a bug in our code" or "an external API's shape was different than assumed."
