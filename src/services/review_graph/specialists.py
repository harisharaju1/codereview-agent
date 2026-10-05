import asyncio
import logging

import anthropic
from langgraph.runtime import Runtime

from src.schemas.triage import FileChange
from src.services.review_graph.loop_graph import (
    AgentConfig,
    build_agent_graph,
    recursion_limit_for,
)
from src.services.review_graph.state import (
    LoopState,
    ReviewContext,
    SpecialistInput,
    WorkflowContext,
)

logger = logging.getLogger(__name__)

# WHAT THIS MODULE IS: the four specialist reviewers of the Day 2 workflow,
# and the one wrapper node that runs any of them in isolation.
#
# WHY SPECIALISTS AT ALL (rather than the Week 2 single agent): one agent
# reviewing security, performance, correctness and dependencies at once
# carries one ever-growing transcript and one budget for everything. Each
# specialist here has a narrow brief, only the tools that brief needs,
# only the files in its scope, its own small budget — and runs in
# parallel with the others. Same loop mechanics (build_agent_graph), four
# configurations.
#
# WHY SPLIT BY CONCERN, NOT BY FILE: per-file specialists would cost
# files x concerns runs (a 30-file PR -> 90+ runs) and lose cross-file
# reasoning ("this new route calls this unsanitized helper"). By concern,
# a review costs ~4 runs regardless of size. If a huge PR ever drowns one
# specialist, chunking its files into groups is a change to the routing
# function alone.

# Every specialist's FIRST line names it — "You are the <name> reviewer."
# — which is also how test fakes tell parallel callers apart.
_COMMON_INSTRUCTIONS = """You are one of several specialist reviewers examining the same \
GitHub pull request in parallel. Other specialists cover other concerns, so stay strictly \
within your focus area above.

You are given the PR title, an inventory of every changed file (including files no \
reviewer reads, such as lockfiles and docs, with the reason), and the diffs of the files \
in your scope. Large diffs may be truncated; use get_file_content when you need the full \
file.

Only report issues that this PR introduces or exposes, not pre-existing problems \
elsewhere. Prefer fewer, well-evidenced findings over speculation. Include a line number \
when an issue is on a specific line of the new code.

When several checks are independent of each other (for example, reading three different \
files), request them all in the same turn rather than one per turn — your number of \
turns is limited, and on your final turn you will only be able to submit.

When you are done, call submit_review exactly once with your findings — an empty list \
if you found nothing in your area. Do not describe findings in plain text instead."""


def _prompt(name: str, focus: str) -> str:
    return f"You are the {name} reviewer.\n\nFocus: {focus}\n\n{_COMMON_INSTRUCTIONS}"


# Budgets are deliberately smaller than the generalist's 8: each
# specialist has a narrower job and starts with exactly the diffs it
# needs. The forced final submission (review_tools.tool_choice_for) means
# a specialist that runs out of turns still hands in what it found.
SPECIALISTS: dict[str, AgentConfig] = {
    "security": AgentConfig(
        name="security",
        system_prompt=_prompt(
            "security",
            "injection (SQL, command, template), authentication and authorization "
            "mistakes, secrets or credentials in code or config, unsafe "
            "deserialization, path traversal, SSRF, and sensitive data exposure.",
        ),
        tool_names=frozenset({"get_file_content", "search_codebase"}),
        max_iterations=5,
    ),
    "performance": AgentConfig(
        name="performance",
        system_prompt=_prompt(
            "performance",
            "N+1 queries, blocking calls inside async code, unbounded loops or "
            "memory growth, repeated expensive work that could be done once, and "
            "missing pagination or limits on external calls.",
        ),
        tool_names=frozenset({"get_file_content", "search_codebase"}),
        max_iterations=4,
    ),
    "correctness": AgentConfig(
        name="correctness",
        system_prompt=_prompt(
            "correctness",
            "logic errors, unhandled or swallowed errors, edge cases (empty, "
            "None, off-by-one), type mismatches, and problems a real linter "
            "reports — run_linter runs ruff, oxlint or dotnet format on a file.",
        ),
        tool_names=frozenset({"get_file_content", "run_linter", "search_codebase"}),
        max_iterations=6,
    ),
    "dependencies": AgentConfig(
        name="dependencies",
        system_prompt=_prompt(
            "dependencies",
            "dependency changes in this PR's manifests — downgraded or outdated "
            "exact pins, and newly added packages worth a second look. "
            "check_dependency_versions compares root-level pins with each "
            "registry's latest version.",
        ),
        tool_names=frozenset({"check_dependency_versions"}),
        max_iterations=3,
    ),
}

# Compiled once each, at import — the same reasoning as LOOP_GRAPH:
# compiled graphs are stateless and safely shared across concurrent runs.
SPECIALIST_GRAPHS = {name: build_agent_graph(config) for name, config in SPECIALISTS.items()}

# The catchable, per-specialist time budget (see run_specialist). The
# node-level `timeout=` in workflow_graph.py sits a little above this as a
# backstop that should never fire.
SPECIALIST_TIMEOUT_SECONDS = 180.0


# Summary: the first user message a specialist sees: PR title, the
# whole-PR inventory, and the diffs of ONLY the files in its scope.
def specialist_first_message(payload: SpecialistInput) -> str:
    files = [FileChange.model_validate(f) for f in payload["files"]]
    diffs = "\n".join(f.diff_text for f in files)
    return (
        f"Pull request: {payload['pr_title']}\n\n"
        f"Changed files in this PR (inventory):\n{payload['inventory']}\n\n"
        f"Diffs of the {len(files)} file(s) in your scope:\n{diffs}"
    )


def _result(
    name: str,
    status: str,
    *,
    findings: list[dict] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: str | None = None,
) -> dict:
    # One entry in WorkflowState.specialist_results, plus this
    # specialist's token counts for the workflow-wide totals. Unknown
    # counts (None) contribute 0 to the totals — the review's usage is
    # then a lower bound, and usage_by_specialist omits this specialist.
    return {
        "specialist_results": [
            {
                "name": name,
                "status": status,
                "findings": findings or [],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
            }
        ],
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
    }


# Summary: the workflow node that runs ONE specialist — started once per
# Send from the routing step, so several run in parallel. Invokes that
# specialist's own compiled agent graph as a SUBGRAPH, and maps its final
# state into one specialist_results entry. Always returns a result; a
# failure becomes a "failed"/"timed_out" entry, never an exception that
# would take down the other specialists.
#
# WHY A WRAPPER NODE THAT INVOKES THE SUBGRAPH, rather than adding each
# compiled subgraph directly as a node: added directly, parent and
# subgraph must share state keys, and the specialist's message history
# would leak into the parent's state unless keys were carefully kept
# apart. Invoking it from a wrapper makes the boundary one explicit line:
# the subgraph's `messages` never touch the parent; only findings and
# token counts come back.
#
# WHY THE TIMEOUT AND ERROR HANDLING ARE HERE, INSIDE THE NODE — verified,
# not assumed: LangGraph 1.2.12's add_node(error_handler=...) DOES turn a
# failure (including its own NodeTimeoutError) into a state update for a
# node reached by a normal edge — but for a node started by Send, as this
# one is, the handler never fired in testing; the failure propagated and
# failed the whole run. And a node's own `timeout=` raises outside the
# node body, so the node can't catch it. asyncio.timeout + try/except
# here is the layer that actually works for a fan-out task.
#
# Which failures are isolated: the ones a single specialist can hit
# without anything being wrong with the workflow itself — its time budget
# running out, or an Anthropic API error that survived the SDK's own
# per-request retries. Anything else (a bug) still propagates and fails
# the run, loudly. Retries live in ONE place — the Anthropic SDK, per
# request — and deliberately not as a LangGraph retry_policy here: a node
# retry would re-run (and re-pay for) the whole specialist.
async def run_specialist(payload: SpecialistInput, runtime: Runtime[WorkflowContext]) -> dict:
    name = payload["specialist"]
    config = SPECIALISTS[name]
    workflow_context = runtime.context
    context = ReviewContext(
        http_client=workflow_context.http_client,
        anthropic_client=workflow_context.anthropic_client,
        settings=workflow_context.settings,
        installation_token=workflow_context.installation_token,
        owner=workflow_context.owner,
        repo=workflow_context.repo,
        head_sha=payload["head_sha"],
        pr_number=workflow_context.pr_number,
    )
    initial_state: LoopState = {
        "messages": [{"role": "user", "content": specialist_first_message(payload)}],
        "iteration": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "stop_reason": None,
        "findings": None,
        "outcome": None,
    }
    label = f"PR #{workflow_context.pr_number} review [{name}]"
    logger.info("%s: starting with %d file(s)", label, len(payload["files"]))

    try:
        async with asyncio.timeout(SPECIALIST_TIMEOUT_SECONDS):
            final_state = await SPECIALIST_GRAPHS[name].ainvoke(
                initial_state,
                config={"recursion_limit": recursion_limit_for(config)},
                context=context,
            )
    except TimeoutError:
        # KNOWN GAP (Day 2): the tokens this specialist spent before the
        # timeout are unknown — cancelling ainvoke discards the subgraph's
        # partial state. Day 3's plan closes this by streaming the
        # subgraph's updates and counting tokens as each call completes.
        logger.warning("%s: timed out after %.0fs", label, SPECIALIST_TIMEOUT_SECONDS)
        return _result(name, "timed_out", error=f"timed out after {SPECIALIST_TIMEOUT_SECONDS}s")
    except anthropic.APIError as exc:
        logger.warning("%s: Claude API error: %s", label, exc)
        return _result(name, "failed", error=f"Claude API error: {type(exc).__name__}")

    tokens = {
        "input_tokens": final_state["input_tokens"],
        "output_tokens": final_state["output_tokens"],
    }
    if final_state["outcome"] == "submitted":
        logger.info("%s: submitted %d finding(s)", label, len(final_state["findings"]))
        return _result(name, "ok", findings=final_state["findings"], **tokens)
    error = (
        f"stopped without submitting (stop_reason={final_state['stop_reason']!r})"
        if final_state["outcome"] == "no_submit"
        else f"exceeded {config.max_iterations} iterations without submitting"
    )
    logger.warning("%s: %s", label, error)
    return _result(name, "failed", error=error, **tokens)
