import logging

import anthropic
import httpx

from src.config.settings import Settings
from src.schemas.review import ReviewResult, ReviewUsage
from src.services.model_pricing import estimate_cost_usd
from src.services.pull_requests import fetch_pull_request_diff, fetch_pull_request_metadata
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

# WHY THIS MODULE LOGS, WHEN NOTHING ELSE IN THIS PROJECT DOES YET:
# every other piece of this project's control flow is deterministic — a
# GitHub 404 is a GitHub 404, reproducibly, every time. This loop's actual
# path (which tools get called, how many times, why it stopped) is decided
# by the model at runtime and is NOT reproducible from the request alone —
# without a record of what happened during a specific run, a failure like
# "exceeded MAX_ITERATIONS" is close to undiagnosable after the fact. This
# is the first place in the project where that tradeoff is worth the cost;
# it's not yet a project-wide logging setup (see docs/week-2/week-2-day-4.md
# and the earlier product-design conversation about observability being a
# real, currently-unaddressed gap).
logger = logging.getLogger(__name__)


# Summary: the agent loop itself. Sends the PR diff to Claude with the
# five tool definitions (review_tools.TOOL_DEFINITIONS); on each turn,
# executes whatever tools the model requested, feeds the results back, and
# repeats — until the model calls submit_review with a validated findings
# list, or MAX_ITERATIONS is exhausted. Returns both the findings and the
# token usage/estimated cost accumulated across every loop iteration (see
# ReviewResult/ReviewUsage). Exists as the first piece of genuinely
# LLM-driven control flow in this project: which code paths run, and how
# many times, is decided by the model at runtime, not by this project's
# own logic.
async def run_review_agent(
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

    # `messages` is plain dicts throughout, not SDK types — the Anthropic
    # Python SDK accepts both for a request's `messages` param, and this
    # keeps the loop's data structures visible/inspectable rather than
    # requiring SDK type construction for every appended entry.
    messages: list[dict] = [
        {
            "role": "user",
            "content": (f"Pull request #{pr.number}: {pr.title}\n\nDiff:\n{diff_text}"),
        }
    ]

    # Summed across every loop iteration, not just the final call — see
    # ReviewUsage's own comment for why only the last request's usage
    # would silently undercount a multi-turn review.
    total_input_tokens = 0
    total_output_tokens = 0

    # A closure, not a free function, specifically so it can read
    # total_input_tokens/total_output_tokens (and settings) as they stand
    # at the moment it's called — used identically at all three exit
    # points below (success, and both failure modes) so "how usage gets
    # turned into a ReviewUsage" is written once, not three times.
    def _current_usage() -> ReviewUsage:
        return ReviewUsage(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimate_cost_usd(
                settings.anthropic_model, total_input_tokens, total_output_tokens
            ),
        )

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info("PR #%d review: iteration %d/%d", pr_number, iteration, MAX_ITERATIONS)
        response = await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            tool_choice=tool_choice_for(iteration, MAX_ITERATIONS),
            messages=messages,
        )
        logger.info("PR #%d review: stop_reason=%s", pr_number, response.stop_reason)
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        if response.stop_reason != "tool_use":
            # The model finished talking (end_turn, max_tokens, ...)
            # without ever calling submit_review — a real, named failure,
            # not a silent empty findings list. An empty review and "the
            # agent gave up" are very different outcomes and must not
            # look the same to a caller.
            logger.error(
                "PR #%d review: stopped without submit_review (stop_reason=%s)",
                pr_number,
                response.stop_reason,
            )
            raise AgentDidNotSubmitReviewError(
                f"Agent stopped without calling submit_review "
                f"(stop_reason={response.stop_reason!r})",
                usage=_current_usage(),
            )

        # The model needs to see its own prior tool-call requests in the
        # conversation history, or the next response won't make sense in
        # context — appending response.content (not just its text) is
        # what preserves that.
        messages.append({"role": "assistant", "content": response.content})

        turn = await execute_tool_turn(
            [
                ToolCall(id=block.id, name=block.name, input=block.input)
                for block in response.content
                if block.type == "tool_use"
            ],
            client,
            installation_token,
            owner,
            repo,
            pr.head_sha,
            log_label=f"PR #{pr_number} review",
        )

        if turn.submitted_findings is not None:
            usage = _current_usage()
            logger.info(
                "PR #%d review: submitted with %d findings after %d iteration(s) "
                "(%d input tokens, %d output tokens, est. cost=%s)",
                pr_number,
                len(turn.submitted_findings),
                iteration,
                usage.input_tokens,
                usage.output_tokens,
                f"${usage.estimated_cost_usd:.4f}"
                if usage.estimated_cost_usd is not None
                else "unknown",
            )
            return ReviewResult(findings=turn.submitted_findings, usage=usage)

        messages.append({"role": "user", "content": turn.tool_results})

    usage = _current_usage()
    logger.error(
        "PR #%d review: exceeded %d iterations (%d input tokens, %d output tokens, est. cost=%s)",
        pr_number,
        MAX_ITERATIONS,
        usage.input_tokens,
        usage.output_tokens,
        f"${usage.estimated_cost_usd:.4f}" if usage.estimated_cost_usd is not None else "unknown",
    )
    raise AgentExceededMaxIterationsError(
        f"Agent exceeded {MAX_ITERATIONS} iterations without calling submit_review",
        usage=usage,
    )
