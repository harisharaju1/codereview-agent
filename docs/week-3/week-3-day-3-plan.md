# Day 3 Plan — LangChain Side by Side, Streaming, and Measuring Every Engine

## Why this day matters

By the end of Day 2 there are three engines: `loop` (Week 2, hand-rolled), `graph` (the faithful port), and `workflow` (triage + specialists + synthesis). Today adds a fourth variant and, more importantly, **turns opinions into numbers.**

The Month 2 interview story is *"I built the same agent three ways — raw API, LangGraph, and (next week) MCP — here's when I'd use each."* That sentence is only worth saying if the "here's when" part is backed by something: success rates, cost, latency, how many planted bugs each approach actually found, and how hard each was to build and change. Today produces that evidence and writes it down in `docs/week-3/engine-comparison.md`.

It also gives LangChain an honest, hands-on hearing. Most of this week deliberately calls the raw Anthropic SDK inside LangGraph nodes. Today one specialist (security) is rebuilt with LangChain's model and tool wrappers, so the difference between the two *layers* is something you've written code against, not something you read in a blog post.

The day has three parts: **A**, the LangChain specialist; **B**, streaming progress out of the graphs; and **C**, the measurement harness and the comparison write-up.

---

## Part A — The security specialist, rebuilt with LangChain

### What LangChain actually is, relative to what's already built

**Like I'm five:** your TV came with its own remote (the Anthropic SDK). A universal remote (LangChain) works with lots of TVs, so if you get a new TV (switch from Claude to another model), you keep the same remote. But the universal remote sometimes doesn't have the special buttons your own TV's remote has, and you have to learn a new set of buttons.

**Really:** `langchain-core` defines provider-neutral interfaces: chat models, messages, tools. `langchain-anthropic` implements them for Claude (`ChatAnthropic`). LangGraph's prebuilt pieces (`ToolNode`, `tools_condition`, `add_messages`) are written *against those interfaces*. So LangChain is what makes LangGraph's prebuilt helpers usable. It isn't required to use LangGraph at all, as Days 1–2 prove.

### Concept-by-concept mapping (the core of today's learning)

| Concern | Raw SDK (Days 1–2) | LangChain (today) |
|---|---|---|
| Model client | `anthropic.AsyncAnthropic().messages.create(model=..., tools=..., messages=...)` | `ChatAnthropic(model=..., max_tokens=...).bind_tools(tools).ainvoke(messages)` |
| Tool definition | Hand-written JSON Schema dicts (`TOOL_DEFINITIONS`), deliberately narrower than the Python function | `@tool`-decorated functions; the schema is **derived from type hints + docstring**, or from an explicit Pydantic `args_schema` |
| Hiding credentials from the model | Executors *close over* the client/token/owner/repo; the model only sees `{"path"}` | Tool parameters annotated as `ToolRuntime` (or `InjectedState`) are **excluded from the schema** and injected at call time; `runtime.context` gives the same `ReviewContext` |
| Messages | Plain dicts: `{"role": "assistant", "content": [blocks]}` | `SystemMessage`, `HumanMessage`, `AIMessage` (with `.tool_calls`), `ToolMessage(tool_call_id=..., status="error")` |
| Message list reducer | `operator.add` (append) | `add_messages` (append, plus ID-based replace/remove) |
| "Did the model ask for tools?" | `response.stop_reason == "tool_use"`, scan `content` for `tool_use` blocks | `ai_message.tool_calls` (a normalized list of `{"name", "args", "id"}`) |
| Running the tools | `execute_tool_turn` (hand-written) | `ToolNode(tools)` (prebuilt; runs calls concurrently, appends `ToolMessage`s) |
| Tool errors back to the model | `is_error: True` tool results, hand-built | `ToolNode(handle_tool_errors=...)` converts exceptions to error `ToolMessage`s |
| Routing after the model | `route_after_model` (hand-written) | `tools_condition` (prebuilt: `"tools"` if there are tool calls, else `END`) |
| Token usage | `response.usage.input_tokens` / `.output_tokens` | `ai_message.usage_metadata["input_tokens"]` / `["output_tokens"]` |
| Forced final tool | `tool_choice={"type": "tool", "name": "submit_review"}` | `bind_tools(tools, tool_choice="submit_review")` on a second bound model used for the last iteration |

### Design of `lc_specialist.py`

- **Tools:** `get_file_content(path: str, runtime: ToolRuntime) -> str` and `search_codebase(query: str, runtime: ToolRuntime) -> str`, decorated with `@tool`, calling the **same service functions** as the executors (`fetch_file_content`, `search_codebase`). They read the credentials from `runtime.context` and `head_sha` from `runtime.state`. Verify on the day that `ToolRuntime` exposes both `.context` and `.state` in this version. The signature check showed `ToolRuntime` and `InjectedState` both exist in `langgraph.prebuilt`, but not their exact attributes.
- **`submit_review`** as a `@tool` with `args_schema=SubmitReviewArgs`, so the **same Pydantic model** validates in both worlds.
- **Graph:** `call_model` → custom router → `ToolNode` → back to `call_model`, with state `{"messages": Annotated[list[AnyMessage], add_messages], "iteration": int}`.

### A finding to expect (and to write down when it happens)

`tools_condition` covers the textbook loop ("tools if the model asked for any, else end"). This agent's loop has a **special exit**: the `submit_review` call ends the run, and it needs the forced final iteration from Day 1. `tools_condition` can express neither, so a custom `route_after_model` comes back. That's the general lesson: **prebuilt helpers save code exactly as long as your loop is the textbook loop, and the moment your agent has one domain-specific rule, you're writing the routing yourself again**, now against someone else's message types.

**Alternatives for the special exit, to try briefly and record:**
1. **Custom router** (above): simplest and most explicit. The default.
2. **A tool that returns `Command(update={"findings": ...}, goto=END)`.** LangGraph lets tools update graph state and route directly. It's idiomatic, but it hides control flow inside a tool body.
3. **Structured output as the final step:** loop with normal tools, then a last call using `with_structured_output(SubmitReviewArgs)`. Clean separation, but it's always one extra model call.

### Testing: the fake chat model

LangChain ships `GenericFakeChatModel`, which returns scripted `AIMessage`s. **Verified before this plan: its `bind_tools` raises `NotImplementedError`.** The subclass is a few lines:

```python
# sketch
class ToolCallingFakeChatModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        self.bound_tool_kwargs = kwargs    # lets tests assert the forced tool_choice
        return self
```

The scripted `AIMessage`s carry `tool_calls=[...]` and `usage_metadata={...}`, so the usage-summing path is exercised too.

### Costs of the LangChain layer (to record, not to argue about)

- **Dependencies:** `langchain-anthropic` pulls in `langchain-core` and its own transitive set. Record the `uv.lock` diff size. Both packages release roughly weekly (both shipped on 2026-09-29), so that's more upgrade surface.
- **Two message formats in one codebase.** The raw engines speak Anthropic dicts; this one speaks LangChain messages. Anything shared (logging, usage summing, tests) needs an adapter or a duplicate.
- **Schema control.** `@tool` derives schemas from signatures. Week 2's explicit decision was *hand-written, deliberately narrow* schemas. `ToolRuntime` injection restores that narrowness, which is worth noting as "LangChain's answer to the executor-closure problem."
- **Model name for pricing:** `model_pricing.estimate_cost_usd` needs the model ID the API actually reported. With LangChain that's in `ai_message.response_metadata`. Confirm the key name on the day (it's model-specific metadata). Don't assume; Week 2's snapshot-suffix bug came from exactly this kind of assumption.

**Engine name:** `workflow_lc`, identical to `workflow` except that the `security` specialist is the LangChain one. Every other specialist is shared, so any measured difference is attributable to that one swap.

---

## Part B — Streaming progress out of the graphs

**Like I'm five:** instead of waiting in silence until the whole meal is ready, the kitchen calls out "starters done!", "mains in the oven!" as each thing happens.

**Really:** `ainvoke` returns only the final state. `astream(..., stream_mode="updates")` yields each node's update **as it completes**. Verified before this plan: output like `{"triage": ...}`, then `{"run_specialist": {...}}`, then `{"synthesize": ...}`. With `subgraphs=True`, updates from *inside* subgraphs are also yielded, tagged with a namespace saying which subgraph they came from. Verify the exact tuple shape on the day.

### What it's used for today

1. **Replace ad-hoc logging in the graph engines.** Week 2's loop logs from inside the loop body. For the graph engines, the runner consumes the stream and logs one line per node completion (`security › call_model #2 (1,840 in / 212 out)`). Logging lives in one place, not in every node.
2. **Close Day 2's known gap.** A timed-out specialist's token spend was unknown because cancellation discarded its partial state. If `run_specialist` *streams* its subgraph instead of `ainvoke`-ing it, it can sum tokens from each `call_model` update **as they arrive**. When the timeout fires, it knows exactly what was spent so far, and the `input_tokens=None` gap from Day 2 disappears.
3. **Groundwork for Day 5/Week 4:** the same stream is what would push live progress to `GET /reviews/{id}` (for example "3 of 4 specialists done").

### About the v3 streaming API

`astream_events(version="v3")` exists, but is marked **experimental** in 1.2.12 (see the week plan's technology check, which corrects the September note). Today: open it, run it once against the workflow graph, and write two sentences in the day retro on what it offers over `stream_mode="updates"`. Don't build on it. If it stabilizes by Week 4, reconsider.

**Alternative for observability: LangSmith** (LangChain's hosted tracing). It gives rich traces of every node and model call with almost no code. Rejected for now: it ships repository code and diffs to a third-party SaaS, which isn't acceptable for a code-review tool without explicit consent from the repo owner, and Month 3 builds the self-hosted equivalent (OpenTelemetry + Jaeger). Worth naming in interviews as a deliberate choice, not an oversight.

---

## Part C — Measuring every engine

### C1. The test PRs

| PR | Purpose | Exists? |
|---|---|---|
| **Small:** `test/small-readme-pr` | Trivial baseline. Everything should succeed cheaply, and the workflow should make **zero** LLM calls if it's docs-only | Yes |
| **Seeded-bug PR:** new branch `test/seeded-bugs`, 3–4 Python/TS files + one manifest change, with **known, planted issues** | The only PR with *ground truth*. Measures what each engine actually *finds*, not just whether it finishes | Create today |
| **Large:** the `raw-agent-loop` PR (11 files, ~1,179 insertions) | The one Week 2's loop couldn't finish. Stress test | Yes |

**The seeded-bug PR is the most valuable thing built today.**

**Like I'm five:** hide four Easter eggs in the garden, one of each colour, then send each team of kids in and count how many eggs each team finds.

Plant one issue per specialist, written down *before* any engine runs (in `docs/week-3/data/seeded-bugs.md`), each with an exact file and line:
1. **Security:** SQL built with an f-string from a request parameter.
2. **Performance:** `requests.get(...)` / `time.sleep(...)` inside an `async def` (a blocking call in async code; this project's own Week 2 reasoning for `create_subprocess_exec`, turned into a test case).
3. **Correctness:** a swallowed exception (`except Exception: pass`) around a write, or an off-by-one in a slice.
4. **Dependencies:** an exact pin downgraded in `requirements.txt` / `package.json`.

**Scoring:** a planted bug counts as *found* if any finding has the same file and a line within ±3 of the planted line. Also record *other* findings and hand-label a sample as useful or noise. That's a lightweight version of Month 1's eval harness, and a preview of Month 5's eval dashboard. Keep the scoring script tiny; the ground-truth file is the valuable part.

### C2. The harness: `scripts/compare_engines.py`

- **In-process, not over HTTP.** It calls the engine runner functions directly (the same `ENGINES` dict the router uses), after minting an installation token via the existing `installation_token_cache`. That's the pattern the learning plan already names for Month 1's eval harness (calling the same underlying functions a queue worker would, bypassing transport). It avoids cookie handling, and it measures the *engine*, not FastAPI.
- **A counting wrapper around the Anthropic client:** a thin proxy whose `messages.create` increments a counter and delegates. That gives *LLM calls per review* without touching engine code. For `workflow_lc`, count `AIMessage`s in the stream instead.
- **Per run, record:** engine, PR, run number, outcome (`ok` / `partial` + which specialists / `failed` + exception type), findings count by severity, seeded bugs found (seeded PR only), LLM calls, input/output tokens, estimated cost, wall-clock seconds, duplicates removed by synthesize (workflow engines), and the model ID.
- **2 runs per (engine, PR)**, because the model is nondeterministic. One run proves nothing about an engine's typical behavior. Two is the minimum that reveals variance; more costs more. Report both runs, not an average of two.
- **Output:** JSON Lines to `docs/week-3/data/runs-YYYY-MM-DD.jsonl` (raw, append-only), plus a generated Markdown table pasted into `engine-comparison.md`.
- **Cost guard:** `--max-cost-usd` (default `5.00`). The harness keeps a running estimated total and **refuses to start the next run** once it's exceeded. The plan is 4 engines × 3 PRs × 2 runs = 24 reviews. At Sonnet 5's post-intro pricing ($3/$15 per MTok, per the September technology notes), a large-PR review could plausibly cost tens of cents, so estimate first. Run the small PR across all engines, look at the per-run cost, then extrapolate before running everything.

### C3. Optional column: Haiku vs Sonnet for specialists

Week 2 found Haiku didn't batch tool calls and couldn't converge on a big PR. Specialists have **narrower tasks and smaller contexts**, which is exactly where a smaller model might be enough. If the budget allows, run `workflow` once more with specialists on `claude-haiku-4-5` (only `synthesize` is model-free, so everything else changes). If Haiku specialists find the seeded bugs at a fraction of the cost, that's a strong, data-backed Week 4 default and a good interview detail. Adding it means a `specialist_model` override. Keep it a harness-level parameter, not a new `Settings` field, until it's decided.

### C4. Qualitative measures (they matter as much as the numbers)

- **Lines of code** per engine (production code, and test code separately).
- **"Change exercise," done on paper, not merged:** *"Add a fifth reviewer that checks whether tests were added for changed code."*
  - In `loop`: a prompt edit. The reviewer shares the context and budget with everything else, and there's no way to give it its own budget or measure its cost separately.
  - In `workflow`: a new `SpecialistConfig` plus one line in the router. It gets its own budget and cost line, and it can fail without sinking the review.
  - Write down the actual diff you'd make in each.
- **Debuggability:** for the most confusing run of the day, how long did it take to understand what happened in each engine? Streamed node updates vs Week 2's log lines.
- **Where the framework got in the way.** Keep a running list during Days 1–3 (the `GenericFakeChatModel` gap, message-format adapters, anything confusing in the docs). The downsides are half the interview answer.

### C5. `docs/week-3/engine-comparison.md` — structure

1. Setup: PRs, model(s), date, SDK/LangGraph versions, number of runs.
2. The results table, from the JSONL.
3. Seeded-bug recall per engine.
4. Cost and latency per engine; cost per specialist (from `usage_by_specialist`).
5. Qualitative: LOC, the change exercise, debuggability, friction list.
6. **When I'd use each.** One paragraph each, including the Functional API note from Day 1 ("for a pure loop agent in production, I'd seriously consider LangGraph's Functional API: checkpointing without the ceremony").
7. **Decision:** the default engine going forward, and which engine(s) get deleted in Week 4 (per the week plan's Decision 9).

---

## Files

1. `src/services/review_graph/lc_specialist.py` — NEW
2. `src/services/review_graph/specialists.py` — accepts an implementation override for `security`
3. `src/services/review_graph/workflow_graph.py`, `loop_graph.py` — runners consume `astream(stream_mode="updates")` for logging; `run_specialist` streams its subgraph to track partial usage
4. `src/schemas/review.py` — `Engine` gains `"workflow_lc"`
5. `src/services/review_engines.py` — register it
6. `scripts/compare_engines.py` — NEW
7. `docs/week-3/data/seeded-bugs.md`, `runs-*.jsonl`, `docs/week-3/engine-comparison.md` — NEW
8. `pyproject.toml` — `langchain-anthropic`
9. Tests: `tests/test_lc_specialist.py`; streaming-based usage tracking for timed-out specialists (the Day 2 gap test, flipped to expect real numbers)
10. A new GitHub branch + PR: `test/seeded-bugs` (not merged; it exists only to be reviewed)

---

## .NET parallels

- `ChatAnthropic` vs the raw SDK ≈ **Semantic Kernel / Microsoft.Extensions.AI's `IChatClient`** vs calling the Azure OpenAI or Anthropic SDK directly. Same tradeoff: portability and ecosystem helpers vs full control and one less abstraction.
- `@tool` schema derivation ≈ Semantic Kernel's `[KernelFunction]` + `[Description]` attributes generating function schemas from method signatures. `ToolRuntime` injection ≈ SK passing `Kernel`/services into a function without exposing them as parameters.
- The measurement harness ≈ **BenchmarkDotNet** for behavior instead of speed: fixed inputs, repeated runs, raw results kept, and a summary table generated from them rather than written by hand.
- Seeded bugs ≈ **mutation testing** (Stryker.NET): deliberately introduce known defects and measure whether your checks catch them. Same idea, applied to an AI reviewer instead of a test suite.
- Streaming node updates ≈ `IAsyncEnumerable<T>` from an orchestration, or Durable Functions' custom status updates.

---

## Automated verification

- **LangChain specialist** (with `ToolCallingFakeChatModel`): tool call → `ToolMessage` → `submit_review` → findings; an invalid `submit_review` args → error `ToolMessage` → corrected; forced tool choice on the last iteration (asserted via the recorded `bind_tools` kwargs); usage summed from `usage_metadata`; credentials never appear in any tool's generated JSON schema (assert on `tool.args_schema.model_json_schema()` / `tool.tool_call_schema`).
- **`workflow_lc` end to end:** same scenarios as `workflow`, with the security specialist on the fake chat model and the others on the fake Anthropic client.
- **Streaming usage:** a specialist that times out after two model calls reports exactly those two calls' tokens (the Day 2 gap closed).
- **Harness:** the cost guard stops before exceeding `--max-cost-usd` (fake engine returning fixed usage); the seeded-bug scorer matches within ±3 lines and not at ±4.
- `uv run pytest` and `uv run ruff check .` clean.

## Manual verification

```bash
# 1. Price check first: every engine, small PR only, 1 run
uv run python scripts/compare_engines.py --prs <small> --runs 1 --max-cost-usd 1

# 2. Full run once per-run costs are known
uv run python scripts/compare_engines.py \
  --prs <small>,<seeded>,<large> --engines loop,graph,workflow,workflow_lc \
  --runs 2 --max-cost-usd 5

# 3. Look at one streamed workflow run's logs end to end: specialists
#    interleave, each node completion is one line, usage adds up to the total.
```

---

## End-of-day checklist

- [ ] `lc_specialist.py` built, with same-Pydantic-model validation; credentials absent from tool schemas (tested)
- [ ] `workflow_lc` engine registered and tested
- [ ] Graph engines log via `astream(stream_mode="updates")`; timed-out specialists report real partial usage
- [ ] v3 streaming tried once; two sentences recorded
- [ ] `test/seeded-bugs` PR created; ground truth written down *before* any engine ran against it
- [ ] Harness run completed within the cost guard; JSONL saved
- [ ] `engine-comparison.md` written, with a default-engine decision
- [ ] `uv run pytest` / `uv run ruff check .` pass
- [ ] `docs/week-3/week-3-day-3.md` written, with its "Alternatives, Patterns, and Architecture Decisions" section

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Seeded bugs with ground truth written first** is Month 1's eval-harness discipline (label first, then score), applied to this project for the first time. It's also the backbone of Month 5's eval dashboard, so the scoring shape designed today is worth keeping reusable.
- **Keeping raw JSONL and generating the table from it** mirrors "typed boundaries around external data": the raw run is the source of truth, and summaries are derived. It's never the other way round.
- **Measuring before choosing a default** follows the project's standing "build the need when it's real" rule. The default engine is a decision made from data, not from which one was most fun to write.
- **The friction list** is a new habit. Week 2 recorded surprises in retros after the fact; this keeps them *during* the work so the comparison's downsides section isn't reconstructed from memory.
- **Rejecting LangSmith for privacy reasons** echoes Week 1's scoping of the GitHub App to read-only permissions: a code-review tool handles other people's code and should minimize where that code goes.
