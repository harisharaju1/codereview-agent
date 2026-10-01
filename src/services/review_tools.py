import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from src.schemas.review import ReviewFinding, ReviewUsage
from src.services.code_search import search_codebase
from src.services.dependency_check import check_dependency_versions
from src.services.github_content import fetch_file_content
from src.services.linters.dispatch import run_linter

# WHY THIS MODULE EXISTS SEPARATELY FROM review_agent.py:
# Week 3 builds the same review agent more than once — the Week 2
# hand-rolled loop (review_agent.py) and LangGraph versions
# (review_graph/). What this week compares is CONTROL FLOW: who decides
# what runs next, how state is carried between steps, how budget and
# failure are handled. How ONE tool call gets executed is not part of that
# comparison — it's genuinely identical in every engine. Keeping a single
# shared copy here means a bug fix can't land in one engine and not the
# other, so later measurements compare designs, not drift between copies.
# Neither engine owns this module; both import from it.
logger = logging.getLogger(__name__)

# Capped the same way github_retry.py caps retries: not because 8 is a
# magic number, but because SOME finite cap has to exist — an agent loop
# with no ceiling can burn API budget indefinitely if the model never
# converges on submit_review. "Eventually give up loudly" is the same
# principle, applied to a confused LLM instead of a flaky network call.
#
# Lives here (moved from review_agent.py), along with the two exceptions
# below, because it's part of the contract EVERY engine shares: the same
# budget is what makes a loop-vs-graph comparison fair, and the same
# exceptions are what let the router treat every engine identically.
MAX_ITERATIONS = 8


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


SYSTEM_PROMPT = """You are an expert code reviewer analyzing a GitHub pull request diff.

You have access to tools that let you investigate the change beyond what \
the diff text alone shows: read a file's full content, search the \
codebase, check whether touched dependencies are outdated, and run a real \
linter against a changed file. Use them when the diff alone doesn't give \
you enough context to judge whether something is actually a problem.

When several checks are independent of each other (for example, reading \
three different files), request them all in the same turn rather than \
one per turn — your number of turns is limited.

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
# the outer review call already resolved, exactly like run_linter's
# ref-closing-over already did in the original plan — this just makes
# get_file_content consistent with that same reasoning instead of being
# the one exception to it.
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


# Summary: the tool_choice to send on a given iteration — "auto" normally,
# but forced to submit_review on the final allowed iteration.
#
# WHY FORCE THE LAST TURN: without this, the model can ask for yet another
# tool on its final allowed turn, leaving the engine no iteration to act
# on the result — the review then fails with AgentExceededMaxIterationsError
# after spending its whole budget and returns nothing. Forcing
# {"type": "tool", "name": "submit_review"} means the model MUST turn
# everything it has gathered so far into findings ("pencils down, hand in
# what you have"). What this still can't guarantee: if that forced
# submission fails validation, there's no iteration left to self-correct,
# so AgentExceededMaxIterationsError remains possible — but as a rare
# malformed-output failure, not the routine "ran out of time" one.
#
# WHY "auto" IS SENT EXPLICITLY ON NON-FINAL TURNS (rather than omitting
# the parameter): it's the API's default either way, but naming it makes
# every request's tool_choice visible in tests and logs, so the one turn
# that differs is obvious by comparison.
#
# Alternatives considered (see docs/week-3/week-3-day-1-plan.md, A2):
# raising MAX_ITERATIONS (treats the symptom; a bigger PR hits the new cap
# too), telling the model its remaining budget each turn (advisory only),
# and reserving the last TWO iterations for forced submission (costs an
# investigation turn on every review to guard a failure not yet observed).
def tool_choice_for(iteration: int, max_iterations: int) -> dict:
    if iteration >= max_iterations:
        return {"type": "tool", "name": "submit_review"}
    return {"type": "auto"}


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
# what lets TOOL_EXECUTORS below be a plain dict lookup rather than each
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
    # content (see the TOOL_DEFINITIONS note above on why). This executor
    # is the bridge: fetch the content the model never sees, then feed it
    # into the real run_linter(path, content).
    file_content = await fetch_file_content(
        client, installation_token, owner, repo, arguments["path"], ref
    )
    result = await run_linter(arguments["path"], file_content.content)
    return result.model_dump_json()


# submit_review is deliberately NOT in this dict — it isn't a data-fetching
# tool with an executor function at all; it's how an engine recognizes
# "the model is done," handled as a special case in execute_tool_turn
# below rather than dispatched through the same machinery as the other four.
#
# Public (no leading underscore), unlike the executors themselves, because
# it's now imported across modules — a leading underscore would claim a
# privacy it no longer has.
TOOL_EXECUTORS: dict[str, Callable[..., Awaitable[str]]] = {
    "get_file_content": _execute_get_file_content,
    "search_codebase": _execute_search_codebase,
    "check_dependency_versions": _execute_check_dependency_versions,
    "run_linter": _execute_run_linter,
}


# WHY A SMALL ENGINE-NEUTRAL DATACLASS RATHER THAN THE SDK's ToolUseBlock:
# the Week 2 loop holds Anthropic SDK objects (ToolUseBlock), while the
# LangGraph engines hold plain dicts in their state (SDK objects in graph
# state trip a "will be blocked in a future version" deserialization
# warning once checkpointed — see docs/week-3/week-3-day-1-plan.md, C4).
# execute_tool_turn shouldn't care which: each engine converts its own
# representation into ToolCall at the call site, and this module never
# depends on either.
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolTurnResult:
    # Exactly one tool_result block per ToolCall, in order — ready to be
    # sent back as the content of the next "user" message.
    tool_results: list[dict]
    # Set only when a submit_review call in this turn validated
    # successfully; None means "keep going" (no submit_review, or an
    # invalid one that was fed back to the model as an error instead).
    submitted_findings: list[ReviewFinding] | None


# Summary: executes every tool call the model requested in ONE assistant
# turn and returns the matching tool_result blocks, plus the validated
# findings if this turn contained a successful submit_review. Exists so
# every engine runs tools identically — lifted verbatim from the body of
# Week 2's inner per-block loop, so the Week 2 loop's behavior is unchanged.
async def execute_tool_turn(
    tool_calls: list[ToolCall],
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
    log_label: str,
) -> ToolTurnResult:
    tool_results: list[dict] = []
    submitted_findings: list[ReviewFinding] | None = None

    # One assistant message may contain MULTIPLE tool_use blocks
    # (parallel tool use) — including, in principle, submit_review
    # alongside a genuine data-fetching tool in the same turn. Every
    # block requested here must get exactly one corresponding
    # tool_result in the SAME next user message; splitting them across
    # multiple messages, or dropping one, would leave the next request
    # malformed. So this loop always finishes building tool_results for
    # every call before returning, even once submit_review is found.
    for call in tool_calls:
        logger.info("%s: tool call %s(%s)", log_label, call.name, call.input)

        if call.name == "submit_review":
            try:
                args = SubmitReviewArgs.model_validate(call.input)
            except ValidationError as exc:
                logger.warning("%s: submit_review validation failed: %s", log_label, exc)
                # Don't fail the whole review over malformed structured
                # output — feed the validation error back as an error
                # tool_result and let the model try again, bounded by
                # the engine's own iteration cap like everything else.
                # Same "let it self-correct within a bounded budget" idea
                # github_retry.py already uses for a different kind of
                # failure (a flaky network call vs. a model producing
                # invalid JSON).
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": f"Invalid submit_review input: {exc}",
                        "is_error": True,
                    }
                )
                continue
            submitted_findings = args.findings
            tool_results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": "Review submitted."}
            )
            continue

        executor = TOOL_EXECUTORS.get(call.name)
        if executor is None:
            # The model asked for a tool name that isn't one of the
            # five we declared — shouldn't happen (Claude only ever
            # requests tools from the list it was given), but treated
            # as a visible tool error rather than an unhandled
            # KeyError if it somehow does.
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": f"Unknown tool: {call.name}",
                    "is_error": True,
                }
            )
            continue

        try:
            result = await executor(client, installation_token, owner, repo, ref, call.input)
            logger.info("%s: %s succeeded (%d bytes)", log_label, call.name, len(result))
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad: ANY failure in a tool executor
            # (a GitHub 404, a subprocess crash, a KeyError from a
            # malformed argument) must surface to the model as a
            # visible, recoverable tool_result — not crash the whole
            # review. A genuinely failed tool call is information the
            # model can react to (try a different path, or note it
            # couldn't check something); an unhandled exception here
            # would fail the entire review over one bad tool call.
            logger.warning("%s: %s failed: %s", log_label, call.name, exc)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": str(exc),
                    "is_error": True,
                }
            )
            continue

        tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": result})

    return ToolTurnResult(tool_results=tool_results, submitted_findings=submitted_findings)
