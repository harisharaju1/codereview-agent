import logging

import anthropic
import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from src.config.settings import Settings
from src.schemas.review import ReviewFinding, ReviewResult, ReviewUsage
from src.schemas.triage import FileChange
from src.services.model_pricing import estimate_cost_usd
from src.services.pull_requests import fetch_pull_request_diff, fetch_pull_request_metadata
from src.services.review_graph.specialists import (
    SPECIALIST_TIMEOUT_SECONDS,
    run_specialist,
)
from src.services.review_graph.state import SpecialistInput, WorkflowContext, WorkflowState
from src.services.review_graph.synthesize import synthesize
from src.services.review_graph.triage import render_inventory, triage_diff
from src.services.review_tools import AllSpecialistsFailedError

# WHAT THIS MODULE IS: the Day 2 review workflow —
#
#   START -> fetch_context -> triage -(route_to_specialists)-> run_specialist x N
#                                    \-(nothing reviewable)-------------\       |
#                                                                        v      v
#                                                                     synthesize -> END
#
# The Day 1 port proved LangGraph can express the Week 2 loop exactly;
# this graph uses what a graph framework is actually good at: a
# deterministic preprocessing step, parallel fan-out with Send, reducers
# merging concurrent results, subgraphs with isolated context, and
# per-node timeouts. See docs/week-3/week-3-day-2-plan.md.
logger = logging.getLogger(__name__)

# The three code specialists read every reviewable non-manifest file; the
# dependency specialist reads only root-level manifests, and only exists
# in a run when one changed.
_CODE_SPECIALISTS = ("security", "performance", "correctness")


def _label(context: WorkflowContext) -> str:
    return f"PR #{context.pr_number} review [workflow]"


# Summary: fetches the PR's title, head commit and diff — the same two
# GitHub calls the single-agent engines make in their runners, here as a
# node so (from Day 4) its result is checkpointed like every other step.
#
# No LangGraph retry_policy: both calls already go through
# github_retry.call_with_retry. A second retry layer on top would
# multiply attempts (3 x 3 = 9) — the classic retry-storm mistake.
async def fetch_context(state: WorkflowState, runtime: Runtime[WorkflowContext]) -> dict:
    context = runtime.context
    pr = await fetch_pull_request_metadata(
        context.http_client,
        context.installation_token,
        context.owner,
        context.repo,
        context.pr_number,
    )
    diff_text = await fetch_pull_request_diff(
        context.http_client,
        context.installation_token,
        context.owner,
        context.repo,
        context.pr_number,
    )
    return {"pr_title": pr.title, "head_sha": pr.head_sha, "diff_text": diff_text}


# Summary: the no-LLM triage pass (see triage.py), as a node. Replaces the
# raw diff with per-file slices and builds the shared inventory text.
async def triage(state: WorkflowState, runtime: Runtime[WorkflowContext]) -> dict:
    files = triage_diff(state["diff_text"])
    reviewable = [f for f in files if f.skip_reason is None]
    logger.info(
        "%s: triage — %d file(s) changed, %d reviewable",
        _label(runtime.context),
        len(files),
        len(reviewable),
    )
    return {
        "files": [f.model_dump() for f in files],
        "inventory": render_inventory(files),
        "diff_text": "",
    }


# Summary: the routing step — decides which specialists run, and with
# which files. Returns either a list of Send objects (each one starts one
# run_specialist task with its own payload, all in parallel, in the same
# super-step) or the single node name "synthesize" when there's nothing
# for a specialist to read.
#
#   docs-only / lockfile-only PR  -> "synthesize" (ZERO model calls)
#   code only                     -> security, performance, correctness
#   code + root manifest          -> those three + dependencies
#   root manifest only            -> dependencies only
def route_to_specialists(state: WorkflowState) -> list[Send] | str:
    files = [FileChange.model_validate(f) for f in state["files"]]
    reviewable = [f for f in files if f.skip_reason is None]
    code_files = [f for f in reviewable if not f.is_manifest]
    manifests = [f for f in reviewable if f.is_manifest]

    def send(specialist: str, scope: list[FileChange]) -> Send:
        payload: SpecialistInput = {
            "specialist": specialist,
            "pr_title": state["pr_title"],
            "head_sha": state["head_sha"],
            "inventory": state["inventory"],
            "files": [f.model_dump() for f in scope],
        }
        return Send("run_specialist", payload)

    sends = [send(name, code_files) for name in _CODE_SPECIALISTS if code_files]
    if manifests:
        sends.append(send("dependencies", manifests))
    return sends or "synthesize"


# Summary: merges the specialists' results (see synthesize.py) and records
# how the review ended. Runs ONCE, after every scheduled specialist has
# finished — LangGraph waits for the whole fan-out super-step before
# moving on.
async def synthesize_node(state: WorkflowState) -> dict:
    synthesis = synthesize(state["specialist_results"])
    return {
        "findings": [f.model_dump() for f in synthesis.findings],
        "incomplete_specialists": synthesis.incomplete_specialists,
        "dropped_findings": synthesis.dropped_findings,
        # Outcome as DATA, exceptions only in the runner — the same
        # reasoning as the Day 1 port (see loop_graph.run_review_graph).
        "outcome": "all_failed" if synthesis.all_failed else "submitted",
    }


def build_workflow_graph() -> CompiledStateGraph:
    builder = StateGraph(WorkflowState, context_schema=WorkflowContext)
    builder.add_node("fetch_context", fetch_context)
    builder.add_node("triage", triage)
    # input_schema: each run_specialist task receives its own Send payload
    # (SpecialistInput), not the shared WorkflowState — and writes back
    # only reducer-backed keys, so parallel runs merge rather than collide.
    #
    # timeout: a BACKSTOP only, set above run_specialist's own catchable
    # time budget. If this ever fires, the inner timeout failed to cancel
    # something (e.g. a blocking call that ignores cancellation) — a bug,
    # and it fails the run loudly by design.
    builder.add_node(
        "run_specialist",
        run_specialist,
        input_schema=SpecialistInput,
        timeout=SPECIALIST_TIMEOUT_SECONDS + 30,
    )
    builder.add_node("synthesize", synthesize_node)
    builder.add_edge(START, "fetch_context")
    builder.add_edge("fetch_context", "triage")
    builder.add_conditional_edges("triage", route_to_specialists, ["run_specialist", "synthesize"])
    builder.add_edge("run_specialist", "synthesize")
    builder.add_edge("synthesize", END)
    return builder.compile()


WORKFLOW_GRAPH = build_workflow_graph()


def _usage(settings: Settings, input_tokens: int, output_tokens: int) -> ReviewUsage:
    return ReviewUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimate_cost_usd(settings.anthropic_model, input_tokens, output_tokens),
    )


# Summary: the workflow engine's entry point — the same signature as every
# other engine (see review_engines.ENGINES), returning the same
# ReviewResult shape, extended with per-specialist usage and the list of
# specialists that didn't produce a result.
async def run_review_workflow(
    client: httpx.AsyncClient,
    anthropic_client: anthropic.AsyncAnthropic,
    settings: Settings,
    installation_token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> ReviewResult:
    context = WorkflowContext(
        http_client=client,
        anthropic_client=anthropic_client,
        settings=settings,
        installation_token=installation_token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
    )
    initial_state: WorkflowState = {
        "pr_title": "",
        "head_sha": "",
        "diff_text": "",
        "files": [],
        "inventory": "",
        "specialist_results": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "findings": [],
        "incomplete_specialists": [],
        "dropped_findings": 0,
        "outcome": None,
    }
    final_state = await WORKFLOW_GRAPH.ainvoke(initial_state, context=context)

    usage = _usage(settings, final_state["input_tokens"], final_state["output_tokens"])
    usage_by_specialist = {
        result["name"]: _usage(settings, result["input_tokens"], result["output_tokens"])
        for result in final_state["specialist_results"]
        if result["input_tokens"] is not None
    }
    label = _label(context)

    if final_state["outcome"] == "all_failed":
        errors = "; ".join(f"{r['name']}: {r['error']}" for r in final_state["specialist_results"])
        logger.error("%s: every specialist failed (%s)", label, errors)
        raise AllSpecialistsFailedError(f"Every specialist failed — {errors}", usage=usage)

    findings = [ReviewFinding.model_validate(f) for f in final_state["findings"]]
    logger.info(
        "%s: %d finding(s) from %d specialist(s)%s (%d input tokens, %d output tokens, "
        "est. cost=%s)",
        label,
        len(findings),
        len(final_state["specialist_results"]),
        (
            f", incomplete: {', '.join(final_state['incomplete_specialists'])}"
            if final_state["incomplete_specialists"]
            else ""
        ),
        usage.input_tokens,
        usage.output_tokens,
        f"${usage.estimated_cost_usd:.4f}" if usage.estimated_cost_usd is not None else "unknown",
    )
    return ReviewResult(
        findings=findings,
        usage=usage,
        incomplete_specialists=final_state["incomplete_specialists"],
        usage_by_specialist=usage_by_specialist,
        dropped_findings=final_state["dropped_findings"],
    )
