import json
import logging
from collections.abc import Awaitable, Callable

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from src.config.settings import Settings
from src.schemas.review import ReviewFinding, ReviewResult, ReviewUsage
from src.services.code_search import search_codebase
from src.services.dependency_check import check_dependency_versions
from src.services.github_content import fetch_file_content
from src.services.linters.dispatch import run_linter
from src.services.model_pricing import estimate_cost_usd
from src.services.pull_requests import fetch_pull_request_diff, fetch_pull_request_metadata

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

# Capped the same way github_retry.py caps retries: not because 8 is a
# magic number, but because SOME finite cap has to exist — an agent loop
# with no ceiling can burn API budget indefinitely if the model never
# converges on submit_review. "Eventually give up loudly" is the same
# principle, applied to a confused LLM instead of a flaky network call.
MAX_ITERATIONS = 8

SYSTEM_PROMPT = """You are an expert code reviewer analyzing a GitHub pull request diff.

You have access to tools that let you investigate the change beyond what \
the diff text alone shows: read a file's full content, search the \
codebase, check whether touched dependencies are outdated, and run a real \
linter against a changed file. Use them when the diff alone doesn't give \
you enough context to judge whether something is actually a problem.

When you have gathered enough information, call submit_review exactly \
once with your findings. Every finding you report MUST go through \
submit_review — do not describe findings in plain text instead of calling \
it. If you have no findings, call submit_review with an empty list rather \
than skipping it."""


# WHY THESE FIVE SCHEMAS ARE HAND-WRITTEN, NOT DERIVED FROM AN EXISTING
# PYDANTIC MODEL's .model_json_schema():
# for four of these tools, the agent-visible shape is deliberately
# NARROWER than the underlying Python function's real signature (see
# docs/week-2/week-2-day-4-plan.md's "agent-facing tool contract" section)
# — there's no single existing model whose schema would produce
# {"path": string} for run_linter, since the real run_linter(path, content)
# takes content too. Hand-writing keeps that narrowing an explicit,
# visible decision in this file, rather than something achieved by
# constructing a second, schema-only Pydantic model per tool purely to
# call .model_json_schema() on it.
#
# WHY get_file_content and run_linter do NOT expose "ref" AS AN ARGUMENT,
# EVEN THOUGH THE PLAN DOCUMENT ORIGINALLY LISTED IT FOR get_file_content:
# a deliberate deviation from the plan, found while actually writing this
# file. There is exactly one correct ref for an entire review — the PR's
# head commit — and nothing else this agent can access (no "base" ref
# tool, no cross-commit comparison tool) gives the model a legitimate
# second ref to ever supply. Exposing "ref" as a free-text argument would
# only ever invite a hallucinated or stale SHA with no corresponding
# upside, so both file-reading tools close over the same fixed head_sha
# the outer run_review_agent call already resolved, exactly like
# run_linter's ref-closing-over already did in the original plan — this
# just makes get_file_content consistent with that same reasoning instead
# of being the one exception to it.
TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_file_content",
        "description": (
            "Fetch the full text content of a file in this pull request's "
            "repository, at the PR's head commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path, e.g. src/main.py",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_codebase",
        "description": (
            "Search this repository's indexed code for a query string. "
            "Returns matching file paths. Useful for finding other places "
            "a changed symbol is used or defined."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text, e.g. a function or symbol name",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_dependency_versions",
        "description": (
            "Check this repo's root-level dependency manifests "
            "(requirements.txt, pyproject.toml, package.json, *.csproj) "
            "for exact-pinned packages that are behind the latest "
            "published version."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_linter",
        "description": (
            "Run a real linter against a file changed in this PR (ruff "
            "for Python, oxlint for TypeScript/JavaScript, dotnet format "
            "for C#) and return its findings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative file path to lint",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit_review",
        "description": (
            "Submit the final code review as a list of structured "
            "findings. Call this exactly once, when you have enough "
            "information to produce a review — including with an empty "
            "findings list if the PR has no issues worth flagging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": ["integer", "null"]},
                            "category": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "summary": {"type": "string"},
                        },
                        "required": ["file", "category", "severity", "summary"],
                    },
                }
            },
            "required": ["findings"],
        },
    },
]


class SubmitReviewArgs(BaseModel):
    findings: list[ReviewFinding]


# Two distinct, named failure modes rather than one generic exception —
# each means something different to whoever's debugging a failed review.
# AgentDidNotSubmitReviewError: the model gave up/finished talking without
# ever calling submit_review. AgentExceededMaxIterationsError: the model
# kept calling tools past the budget this project is willing to spend on
# one review. Distinguishing them costs nothing and tells a future reader
# (or the router's error handling) which of two very different things
# actually happened.
#
# WHY BOTH CARRY A `usage: ReviewUsage`, ADDED AFTER THE FACT:
# a failed review still spends real, billed tokens — every iteration up to
# the failure point already called the Claude API. Without this, a caller
# would have no way to see what a *failed* review cost, only a successful
# one (ReviewResult.usage) — an asymmetry found directly while testing: a
# real run against a large PR hit AgentExceededMaxIterationsError, and the
# tokens spent getting there were real but invisible anywhere in the
# response. Attaching usage to the exception itself, rather than inventing
# a second response shape for failures, means the router only has to reach
# `exc.usage` to surface it, wherever it ends up in the error response.
class AgentDidNotSubmitReviewError(Exception):
    def __init__(self, message: str, usage: ReviewUsage):
        super().__init__(message)
        self.usage = usage


class AgentExceededMaxIterationsError(Exception):
    def __init__(self, message: str, usage: ReviewUsage):
        super().__init__(message)
        self.usage = usage


def _dump_list(models: list[BaseModel]) -> str:
    # A tool_result's content must be a string (see ToolResultBlockParam)
    # — this is the list-of-models equivalent of a single model's
    # .model_dump_json(), for the two tools (search_codebase,
    # check_dependency_versions) whose service functions return a list
    # rather than one Pydantic object.
    return json.dumps([model.model_dump(mode="json") for model in models])


# Every executor shares the exact same signature — (client,
# installation_token, owner, repo, ref, arguments) -> str — even though
# several of them don't use every parameter (search_codebase's real
# service call has no notion of "ref" at all). A uniform signature is
# what lets _TOOL_EXECUTORS below be a plain dict lookup rather than each
# call site needing to know which specific parameters a given tool
# actually needs.
async def _execute_get_file_content(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
    arguments: dict,
) -> str:
    file_content = await fetch_file_content(
        client, installation_token, owner, repo, arguments["path"], ref
    )
    return file_content.model_dump_json()


async def _execute_search_codebase(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
    arguments: dict,
) -> str:
    results = await search_codebase(client, installation_token, owner, repo, arguments["query"])
    return _dump_list(results)


async def _execute_check_dependency_versions(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
    arguments: dict,
) -> str:
    results = await check_dependency_versions(client, installation_token, owner, repo, ref)
    return _dump_list(results)


async def _execute_run_linter(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
    arguments: dict,
) -> str:
    # The agent asks to lint a path; it never handles the file's actual
    # content (see this module's own top-of-file note on why). This
    # executor is the bridge: fetch the content the model never sees,
    # then feed it into the real run_linter(path, content).
    file_content = await fetch_file_content(
        client, installation_token, owner, repo, arguments["path"], ref
    )
    result = await run_linter(arguments["path"], file_content.content)
    return result.model_dump_json()


# submit_review is deliberately NOT in this dict — it isn't a data-fetching
# tool with an executor function at all; it's how the loop below recognizes
# "the model is done," handled as a special case rather than dispatched
# through the same machinery as the other four.
_TOOL_EXECUTORS: dict[str, Callable[..., Awaitable[str]]] = {
    "get_file_content": _execute_get_file_content,
    "search_codebase": _execute_search_codebase,
    "check_dependency_versions": _execute_check_dependency_versions,
    "run_linter": _execute_run_linter,
}


# Summary: the agent loop itself. Sends the PR diff to Claude with the
# five tool definitions above; on each turn, executes whatever tools the
# model requested, feeds the results back, and repeats — until the model
# calls submit_review with a validated findings list, or MAX_ITERATIONS is
# exhausted. Returns both the findings and the token usage/estimated cost
# accumulated across every loop iteration (see ReviewResult/ReviewUsage).
# Exists as the first piece of genuinely LLM-driven control flow in this
# project: which code paths run, and how many times, is decided by the
# model at runtime, not by this project's own logic.
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

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        tool_results: list[dict] = []
        submitted_findings: list[ReviewFinding] | None = None

        # One assistant message may contain MULTIPLE tool_use blocks
        # (parallel tool use) — including, in principle, submit_review
        # alongside a genuine data-fetching tool in the same turn. Every
        # block requested here must get exactly one corresponding
        # tool_result in the SAME next user message; splitting them across
        # multiple messages, or dropping one, would leave the next request
        # malformed. So this loop always finishes building tool_results for
        # every block before returning, even once submit_review is found.
        for block in tool_use_blocks:
            logger.info("PR #%d review: tool call %s(%s)", pr_number, block.name, block.input)

            if block.name == "submit_review":
                try:
                    args = SubmitReviewArgs.model_validate(block.input)
                except ValidationError as exc:
                    logger.warning(
                        "PR #%d review: submit_review validation failed: %s", pr_number, exc
                    )
                    # Don't fail the whole review over malformed structured
                    # output — feed the validation error back as an error
                    # tool_result and let the model try again, bounded by
                    # the same MAX_ITERATIONS cap as everything else. Same
                    # "let it self-correct within a bounded budget" idea
                    # github_retry.py already uses for a different kind of
                    # failure (a flaky network call vs. a model producing
                    # invalid JSON).
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Invalid submit_review input: {exc}",
                            "is_error": True,
                        }
                    )
                    continue
                submitted_findings = args.findings
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Review submitted.",
                    }
                )
                continue

            executor = _TOOL_EXECUTORS.get(block.name)
            if executor is None:
                # The model asked for a tool name that isn't one of the
                # five we declared — shouldn't happen (Claude only ever
                # requests tools from the list it was given), but treated
                # as a visible tool error rather than an unhandled
                # KeyError if it somehow does.
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Unknown tool: {block.name}",
                        "is_error": True,
                    }
                )
                continue

            try:
                result = await executor(
                    client, installation_token, owner, repo, pr.head_sha, block.input
                )
                logger.info(
                    "PR #%d review: %s succeeded (%d bytes)", pr_number, block.name, len(result)
                )
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad: ANY failure in a tool executor
                # (a GitHub 404, a subprocess crash, a KeyError from a
                # malformed argument) must surface to the model as a
                # visible, recoverable tool_result — not crash the whole
                # loop. A genuinely failed tool call is information the
                # model can react to (try a different path, or note it
                # couldn't check something); an unhandled exception here
                # would fail the entire review over one bad tool call.
                logger.warning("PR #%d review: %s failed: %s", pr_number, block.name, exc)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(exc),
                        "is_error": True,
                    }
                )
                continue

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        if submitted_findings is not None:
            usage = _current_usage()
            logger.info(
                "PR #%d review: submitted with %d findings after %d iteration(s) "
                "(%d input tokens, %d output tokens, est. cost=%s)",
                pr_number,
                len(submitted_findings),
                iteration,
                usage.input_tokens,
                usage.output_tokens,
                f"${usage.estimated_cost_usd:.4f}"
                if usage.estimated_cost_usd is not None
                else "unknown",
            )
            return ReviewResult(
                findings=submitted_findings,
                usage=usage,
            )

        messages.append({"role": "user", "content": tool_results})

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
