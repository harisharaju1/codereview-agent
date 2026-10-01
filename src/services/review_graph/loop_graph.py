import logging

import anthropic
import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from src.config.settings import Settings
from src.schemas.review import ReviewFinding, ReviewResult, ReviewUsage
from src.services.model_pricing import estimate_cost_usd
from src.services.pull_requests import fetch_pull_request_diff, fetch_pull_request_metadata
from src.services.review_graph.state import LoopState, ReviewContext
from src.services.review_tools import (
    MAX_ITERATIONS,
    SYSTEM_PROMPT,
    TOOL_DEFINITIONS,
    AgentDidNotSubmitReviewError,
    AgentExceededMaxIterationsError,
    ToolCall,
    execute_tool_turn,
    tool_choice_for,
)

# WHAT THIS MODULE IS: a deliberately FAITHFUL, line-for-line port of the
# Week 2 loop (review_agent.py) into a LangGraph graph — same prompt, same
# tools, same budget, same forced final submission, same outputs, same
# exceptions. It is not supposed to be better yet. The point is that every
# Week 2 test scenario passes against BOTH engines unchanged, so later
# differences (Day 2's specialists) can be blamed on ARCHITECTURE, not on
# "LangGraph did something subtly different." See
# docs/week-3/week-3-day-1-plan.md, Part D.
#
# THE SHAPE, compared with the Week 2 loop:
#   for iteration in ...:           ->  call_model node, then route_after_model
#       response = create(...)
#       if not tool_use: raise      ->  outcome="no_submit", route to END
#       run the tools               ->  run_tools node, then route_after_tools
#       if submitted: return        ->  outcome="submitted", route to END
#   raise exceeded                  ->  outcome="exceeded", route to END
# The `for` loop's control flow is now explicit edges, declared in one
# place (build_loop_graph) and drawable as a diagram.
logger = logging.getLogger(__name__)

# WHY THIS NUMBER: LangGraph runs in "super-steps" (rounds in which every
# scheduled node runs, then all their updates are applied), and caps them
# per run at `recursion_limit` (default 25), raising GraphRecursionError
# past it. One Week 2 iteration = two super-steps here (call_model +
# run_tools), so MAX_ITERATIONS iterations need about 2*MAX_ITERATIONS.
# The +5 headroom guarantees the PROJECT's own cap (MAX_ITERATIONS, with
# its named exception and attached usage) always fires first; the
# framework's cap stays as a safety net that should never fire. If
# GraphRecursionError ever does fire, it means the routing below has a
# bug — e.g. an edge that never reaches END.
RECURSION_LIMIT = 2 * MAX_ITERATIONS + 5


def _log_label(context: ReviewContext) -> str:
    return f"PR #{context.pr_number} review [graph]"


# Summary: one model call — the body of one Week 2 loop iteration up to the
# point of running tools. Returns only the keys it changes; the reducers
# in LoopState append the assistant message and add the token counts.
async def call_model(state: LoopState, runtime: Runtime[ReviewContext]) -> dict:
    context = runtime.context
    iteration = state["iteration"] + 1
    logger.info("%s: iteration %d/%d", _log_label(context), iteration, MAX_ITERATIONS)

    response = await context.anthropic_client.messages.create(
        model=context.settings.anthropic_model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=TOOL_DEFINITIONS,
        tool_choice=tool_choice_for(iteration, MAX_ITERATIONS),
        messages=state["messages"],
    )
    logger.info("%s: stop_reason=%s", _log_label(context), response.stop_reason)

    update: dict = {
        "iteration": iteration,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "stop_reason": response.stop_reason,
    }

    if response.stop_reason != "tool_use":
        # Same failure the Week 2 loop raises AgentDidNotSubmitReviewError
        # for — recorded here as an outcome, not raised (see run_review_graph).
        logger.error(
            "%s: stopped without submit_review (stop_reason=%s)",
            _log_label(context),
            response.stop_reason,
        )
        update["outcome"] = "no_submit"
        return update

    # The model needs its own prior tool-call requests in the history or
    # the next response won't make sense — so the assistant turn is
    # appended, exactly like the Week 2 loop. The difference: converted to
    # plain dicts at this boundary (model_dump), because this value lives
    # in graph state, and SDK objects in state don't survive checkpointing
    # cleanly. exclude_none drops optional SDK fields that are unset (e.g.
    # TextBlock.citations), so what gets sent back to the API next turn is
    # exactly the block's real content, with no explicit nulls.
    update["messages"] = [
        {
            "role": "assistant",
            "content": [block.model_dump(exclude_none=True) for block in response.content],
        }
    ]
    return update


# Summary: runs every tool the model asked for in its latest turn, through
# the SAME execute_tool_turn the Week 2 loop uses, and either records a
# successful submission or appends the tool results for the next turn.
async def run_tools(state: LoopState, runtime: Runtime[ReviewContext]) -> dict:
    context = runtime.context
    last_assistant_message = state["messages"][-1]
    tool_calls = [
        ToolCall(id=block["id"], name=block["name"], input=block["input"])
        for block in last_assistant_message["content"]
        if block["type"] == "tool_use"
    ]

    turn = await execute_tool_turn(
        tool_calls,
        context.http_client,
        context.installation_token,
        context.owner,
        context.repo,
        context.head_sha,
        log_label=_log_label(context),
    )

    if turn.submitted_findings is not None:
        return {
            "findings": [finding.model_dump() for finding in turn.submitted_findings],
            "outcome": "submitted",
        }

    update: dict = {"messages": [{"role": "user", "content": turn.tool_results}]}
    if state["iteration"] >= MAX_ITERATIONS:
        # The Week 2 loop's `for` simply runs out here; in the graph, "no
        # iterations left" has to be an explicit decision, made right
        # after the last turn's tools ran — matching the loop exactly
        # (the tool results are still appended, then the run ends).
        update["outcome"] = "exceeded"
    return update


# Routing functions: plain functions of state that name the next node.
# They hold all of the control flow that lived in the Week 2 loop's `if`s,
# and they're kept OUT of the node bodies on purpose (the alternative —
# nodes returning Command(goto=...) — spreads routing across node code).
# With conditional edges, nodes only compute and every routing decision is
# declared in build_loop_graph, which is also exactly what draw_mermaid()
# draws.
def route_after_model(state: LoopState) -> str:
    return END if state["outcome"] is not None else "run_tools"


def route_after_tools(state: LoopState) -> str:
    return END if state["outcome"] is not None else "call_model"


# Summary: declares the loop as a graph and compiles it.
#
#   START -> call_model -(tool_use)-> run_tools -(keep going)-> call_model
#                       \-(no tool_use)-> END   \-(submitted / exceeded)-> END
#
# The third argument to add_conditional_edges lists every node a router
# may return, so LangGraph can validate the graph and draw it without
# running it.
def build_loop_graph() -> CompiledStateGraph:
    builder = StateGraph(LoopState, context_schema=ReviewContext)
    builder.add_node("call_model", call_model)
    builder.add_node("run_tools", run_tools)
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges("call_model", route_after_model, ["run_tools", END])
    builder.add_conditional_edges("run_tools", route_after_tools, ["call_model", END])
    return builder.compile()


# Compiled ONCE at import. Compiling validates the structure and produces a
# stateless, reusable runnable — all per-run data arrives via the input
# state and `context`, so one instance safely serves every concurrent
# request. (Day 4 moves compilation into app startup, because the Postgres
# checkpointer it will be compiled with only exists once the pool opens.)
LOOP_GRAPH = build_loop_graph()


# Summary: the graph engine's entry point — SAME signature as the Week 2
# loop's run_review_agent, which is what lets both sit side by side in
# review_engines.ENGINES and be chosen per request. Fetches the PR (here,
# not in a node, to mirror Week 2 exactly; Day 2's workflow moves this into
# a node so it gets checkpointed), runs the graph, and turns its final
# state into the same ReviewResult or the same two exceptions the loop
# produces — so the router can't tell the engines apart.
#
# WHY OUTCOMES ARE DATA INSIDE THE GRAPH AND ONLY BECOME EXCEPTIONS HERE:
# "the model never submitted" and "the budget ran out" are expected
# business outcomes, not crashes. Recorded as state, the graph always ends
# normally at END. That matters from Day 4: a normally-ended graph writes
# a final checkpoint that RECORDS the failure, whereas a graph killed by an
# exception leaves its last checkpoint looking in-progress, and a resume
# would re-run (and re-pay for) the failing step only to reach the same
# deterministic failure. It also keeps business outcomes away from
# LangGraph's retry_policy/error_handler, which act on exceptions raised
# in nodes — only genuinely unexpected failures should be eligible for
# those. Exceptions remain the right tool for the unexpected (e.g. an
# Anthropic outage after the SDK's own retries), which still propagate.
async def run_review_graph(
    client: httpx.AsyncClient,
    anthropic_client: anthropic.AsyncAnthropic,
    settings: Settings,
    installation_token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> ReviewResult:
    pr = await fetch_pull_request_metadata(client, installation_token, owner, repo, pr_number)
    diff_text = await fetch_pull_request_diff(client, installation_token, owner, repo, pr_number)

    context = ReviewContext(
        http_client=client,
        anthropic_client=anthropic_client,
        settings=settings,
        installation_token=installation_token,
        owner=owner,
        repo=repo,
        head_sha=pr.head_sha,
        pr_number=pr_number,
    )
    initial_state: LoopState = {
        "messages": [
            {
                "role": "user",
                "content": f"Pull request #{pr.number}: {pr.title}\n\nDiff:\n{diff_text}",
            }
        ],
        "iteration": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "stop_reason": None,
        "findings": None,
        "outcome": None,
    }

    final_state = await LOOP_GRAPH.ainvoke(
        initial_state,
        config={"recursion_limit": RECURSION_LIMIT},
        context=context,
    )

    usage = ReviewUsage(
        input_tokens=final_state["input_tokens"],
        output_tokens=final_state["output_tokens"],
        estimated_cost_usd=estimate_cost_usd(
            settings.anthropic_model, final_state["input_tokens"], final_state["output_tokens"]
        ),
    )
    cost_text = (
        f"${usage.estimated_cost_usd:.4f}" if usage.estimated_cost_usd is not None else "unknown"
    )

    match final_state["outcome"]:
        case "submitted":
            findings = [ReviewFinding.model_validate(f) for f in final_state["findings"]]
            logger.info(
                "%s: submitted with %d findings after %d iteration(s) "
                "(%d input tokens, %d output tokens, est. cost=%s)",
                _log_label(context),
                len(findings),
                final_state["iteration"],
                usage.input_tokens,
                usage.output_tokens,
                cost_text,
            )
            return ReviewResult(findings=findings, usage=usage)
        case "no_submit":
            raise AgentDidNotSubmitReviewError(
                f"Agent stopped without calling submit_review "
                f"(stop_reason={final_state['stop_reason']!r})",
                usage=usage,
            )
        case _:
            logger.error(
                "%s: exceeded %d iterations (%d input tokens, %d output tokens, est. cost=%s)",
                _log_label(context),
                MAX_ITERATIONS,
                usage.input_tokens,
                usage.output_tokens,
                cost_text,
            )
            raise AgentExceededMaxIterationsError(
                f"Agent exceeded {MAX_ITERATIONS} iterations without calling submit_review",
                usage=usage,
            )
