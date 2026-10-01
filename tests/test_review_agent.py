import copy
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from anthropic.types import ToolUseBlock
from pydantic import BaseModel

from src.config.settings import get_settings
from src.services import review_tools
from src.services.review_engines import ENGINES

PR_URL = "https://api.github.com/repos/owner/repo/pulls/7"

# WHAT THIS FILE TESTS: every scenario below runs against EVERY engine in
# review_engines.ENGINES (the Week 2 "loop" and the Week 3 LangGraph
# "graph" port), via the parametrized `engine` fixture. The same scripted
# model responses must produce the same outcome from both — that's the
# evidence the LangGraph port is faithful (docs/week-3/week-3-day-1-plan.md).
#
# WHY CONTENT BLOCKS ARE REAL anthropic.types.ToolUseBlock OBJECTS (Week 2
# used a hand-written FakeToolUseBlock dataclass): the graph engine
# converts every block with `.model_dump(exclude_none=True)` before putting
# it in graph state. A hand-written fake would only prove that conversion
# works against this project's ASSUMPTIONS about the SDK's shape; the real
# type proves it against what production actually receives — Week 2's
# retrospective lesson about fakes that mirror assumptions rather than
# reality. The response wrapper (FakeMessage) stays a small dataclass:
# both engines only read `.content`, `.stop_reason` and `.usage` from it.


def FakeToolUseBlock(id: str, name: str, input: dict) -> ToolUseBlock:  # noqa: N802
    # Kept under its Week 2 name so the scenarios below read unchanged.
    return ToolUseBlock(type="tool_use", id=id, name=name, input=input)


@dataclass
class FakeUsage:
    # Small, fixed per-response token counts — good enough to prove
    # each engine sums usage across iterations (see the usage assertions
    # in test_single_tool_call_then_submit_review below), without
    # needing to match any real Anthropic response's actual numbers.
    input_tokens: int = 100
    output_tokens: int = 20


@dataclass
class FakeMessage:
    content: list
    stop_reason: str = "tool_use"
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeMessages:
    def __init__(self, responses: list[FakeMessage]):
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        # deepcopy, not the kwargs themselves: the Week 2 loop keeps
        # appending to the SAME `messages` list object after each call, so
        # without a snapshot every recorded call would show the final
        # conversation rather than what was actually sent at the time —
        # which would make per-call comparisons between engines meaningless.
        self.calls.append(copy.deepcopy(kwargs))
        return next(self._responses)


@dataclass
class FakeAnthropicClient:
    responses: list[FakeMessage] = field(default_factory=list)

    def __post_init__(self):
        self.messages = FakeMessages(self.responses)


def _mock_pr_fetch(respx_mock):
    respx_mock.get(PR_URL).mock(
        return_value=httpx.Response(
            200, json={"number": 7, "title": "Add feature", "head": {"sha": "abc123"}}
        )
    )
    respx_mock.get(PR_URL, headers={"Accept": "application/vnd.github.v3.diff"}).mock(
        return_value=httpx.Response(200, text="diff --git a/x b/x\n")
    )


@pytest.fixture(params=list(ENGINES))
def engine(request) -> str:
    return request.param


async def _run(anthropic_client, engine: str):
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        return await ENGINES[engine](
            client, anthropic_client, settings, "installation-token", "owner", "repo", 7
        )


async def test_single_tool_call_then_submit_review(respx_mock, engine):
    _mock_pr_fetch(respx_mock)
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/README.md", params={"ref": "abc123"}
    ).mock(
        return_value=httpx.Response(
            200, json={"path": "README.md", "content": "aGVsbG8=", "encoding": "base64"}
        )
    )

    fake_client = FakeAnthropicClient(
        responses=[
            FakeMessage(
                content=[
                    FakeToolUseBlock(id="t1", name="get_file_content", input={"path": "README.md"})
                ]
            ),
            FakeMessage(
                content=[
                    FakeToolUseBlock(
                        id="t2",
                        name="submit_review",
                        input={
                            "findings": [
                                {
                                    "file": "README.md",
                                    "category": "docs",
                                    "severity": "low",
                                    "summary": "Consider adding a usage example.",
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )

    result = await _run(fake_client, engine)

    assert len(result.findings) == 1
    assert result.findings[0].file == "README.md"
    assert result.findings[0].severity == "low"
    assert len(fake_client.messages.calls) == 2
    # Two calls, FakeUsage's defaults (100 input / 20 output) each — proves
    # usage is SUMMED across iterations, not just the final call's.
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 40
    assert result.usage.estimated_cost_usd is not None


async def test_multiple_sequential_tool_calls_before_submit(respx_mock, engine):
    _mock_pr_fetch(respx_mock)
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/search/code",
        params={"q": "TODO repo:owner/repo"},
    ).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    )

    fake_client = FakeAnthropicClient(
        responses=[
            FakeMessage(
                content=[FakeToolUseBlock(id="t1", name="search_codebase", input={"query": "TODO"})]
            ),
            FakeMessage(
                content=[FakeToolUseBlock(id="t2", name="check_dependency_versions", input={})]
            ),
            FakeMessage(
                content=[FakeToolUseBlock(id="t3", name="submit_review", input={"findings": []})]
            ),
        ]
    )
    # check_dependency_versions needs the root-contents listing mocked too
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents", params={"ref": "abc123"}
    ).mock(return_value=httpx.Response(200, json=[]))

    result = await _run(fake_client, engine)

    assert result.findings == []
    assert len(fake_client.messages.calls) == 3


async def test_submit_review_self_corrects_after_invalid_input(respx_mock, engine):
    _mock_pr_fetch(respx_mock)

    fake_client = FakeAnthropicClient(
        responses=[
            FakeMessage(
                content=[
                    FakeToolUseBlock(
                        id="t1",
                        name="submit_review",
                        # Missing required "severity" — invalid against
                        # SubmitReviewArgs/ReviewFinding.
                        input={
                            "findings": [{"file": "x.py", "category": "bug", "summary": "oops"}]
                        },
                    )
                ]
            ),
            FakeMessage(
                content=[
                    FakeToolUseBlock(
                        id="t2",
                        name="submit_review",
                        input={
                            "findings": [
                                {
                                    "file": "x.py",
                                    "category": "bug",
                                    "severity": "high",
                                    "summary": "oops",
                                }
                            ]
                        },
                    )
                ]
            ),
        ]
    )

    result = await _run(fake_client, engine)

    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"
    # Confirms the retry actually happened via the loop, not a fluke of
    # only ever needing one call.
    assert len(fake_client.messages.calls) == 2
    # The second request's messages must contain the fed-back validation
    # error as a tool_result, proving self-correction context was passed.
    second_call_messages = fake_client.messages.calls[1]["messages"]
    tool_result_contents = [
        block["content"]
        for msg in second_call_messages
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if block.get("type") == "tool_result"
    ]
    assert any("Invalid submit_review input" in content for content in tool_result_contents)


async def test_exceeds_max_iterations_raises(respx_mock, engine):
    _mock_pr_fetch(respx_mock)
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/search/code",
        params={"q": "x repo:owner/repo"},
    ).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    )

    # A fake client that always requests another tool call, never submits.
    responses = [
        FakeMessage(
            content=[FakeToolUseBlock(id=f"t{i}", name="search_codebase", input={"query": "x"})]
        )
        for i in range(review_tools.MAX_ITERATIONS)
    ]
    fake_client = FakeAnthropicClient(responses=responses)

    with pytest.raises(review_tools.AgentExceededMaxIterationsError) as exc_info:
        await _run(fake_client, engine)

    assert len(fake_client.messages.calls) == review_tools.MAX_ITERATIONS
    # A failed review still spent real tokens getting there — usage must
    # be attached to the exception itself, not lost because no
    # ReviewResult was ever constructed.
    usage = exc_info.value.usage
    assert usage.input_tokens == 100 * review_tools.MAX_ITERATIONS
    assert usage.output_tokens == 20 * review_tools.MAX_ITERATIONS
    assert usage.estimated_cost_usd is not None


async def test_final_iteration_forces_submit_review_and_succeeds(respx_mock, engine):
    _mock_pr_fetch(respx_mock)
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/search/code",
        params={"q": "x repo:owner/repo"},
    ).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    )

    # Keeps investigating on every non-final turn; on the final turn (where
    # the real API would be forcing submit_review) it submits. The fake
    # doesn't enforce tool_choice itself — what's under test is that the
    # loop REQUESTS the forced choice on exactly the last iteration, and
    # that a submission on that last iteration still counts as success
    # rather than tipping over into AgentExceededMaxIterationsError.
    responses = [
        FakeMessage(
            content=[FakeToolUseBlock(id=f"t{i}", name="search_codebase", input={"query": "x"})]
        )
        for i in range(review_tools.MAX_ITERATIONS - 1)
    ] + [
        FakeMessage(
            content=[FakeToolUseBlock(id="final", name="submit_review", input={"findings": []})]
        )
    ]
    fake_client = FakeAnthropicClient(responses=responses)

    result = await _run(fake_client, engine)

    assert result.findings == []
    calls = fake_client.messages.calls
    assert len(calls) == review_tools.MAX_ITERATIONS
    assert calls[-1]["tool_choice"] == {"type": "tool", "name": "submit_review"}
    assert all(call["tool_choice"] == {"type": "auto"} for call in calls[:-1])


async def test_agent_stopping_without_tool_use_raises(engine):
    fake_client = FakeAnthropicClient(responses=[FakeMessage(content=[], stop_reason="end_turn")])

    import respx

    with respx.mock:
        _mock_pr_fetch(respx.mock)
        with pytest.raises(review_tools.AgentDidNotSubmitReviewError):
            await _run(fake_client, engine)


async def test_failing_tool_executor_surfaces_as_error_tool_result(respx_mock, engine):
    _mock_pr_fetch(respx_mock)
    # The contents call returns a GitHub 404, which the executor raises —
    # execute_tool_turn's try/except (shared by both engines) must catch
    # it and convert it into an is_error tool_result rather than letting
    # it crash the review.
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/missing.py", params={"ref": "abc123"}
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    fake_client = FakeAnthropicClient(
        responses=[
            FakeMessage(
                content=[
                    FakeToolUseBlock(id="t1", name="get_file_content", input={"path": "missing.py"})
                ]
            ),
            FakeMessage(
                content=[FakeToolUseBlock(id="t2", name="submit_review", input={"findings": []})]
            ),
        ]
    )

    result = await _run(fake_client, engine)

    assert result.findings == []
    second_call_messages = fake_client.messages.calls[1]["messages"]
    tool_result = next(
        block
        for msg in second_call_messages
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if block.get("type") == "tool_result" and block["tool_use_id"] == "t1"
    )
    assert tool_result["is_error"] is True


def _normalize(value):
    # The Week 2 loop sends the SDK's own block objects back in `messages`;
    # the graph sends the same blocks as plain dicts (model_dump). Both are
    # accepted by the Anthropic API — normalizing to dicts here is what
    # lets the test ask "did both engines send the same REQUEST?" rather
    # than tripping over that representation difference.
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


async def test_engines_send_identical_requests_for_the_same_script(respx_mock):
    # The strongest parity check: not just "same final result," but the
    # exact same sequence of Messages API requests (model, prompt, tools,
    # tool_choice, and the full conversation at each call) from both
    # engines, across a tool call, an invalid submission fed back as an
    # error, and a corrected submission. If the port ever drifts from the
    # loop's behavior in any way the model could observe, this fails.
    _mock_pr_fetch(respx_mock)
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/README.md", params={"ref": "abc123"}
    ).mock(
        return_value=httpx.Response(
            200, json={"path": "README.md", "content": "aGVsbG8=", "encoding": "base64"}
        )
    )

    def script() -> list[FakeMessage]:
        return [
            FakeMessage(
                content=[
                    FakeToolUseBlock(id="t1", name="get_file_content", input={"path": "README.md"})
                ]
            ),
            FakeMessage(
                content=[
                    FakeToolUseBlock(
                        id="t2",
                        name="submit_review",
                        input={"findings": [{"file": "README.md", "summary": "no severity"}]},
                    )
                ]
            ),
            FakeMessage(
                content=[FakeToolUseBlock(id="t3", name="submit_review", input={"findings": []})]
            ),
        ]

    requests_by_engine = {}
    for engine_name in ENGINES:
        fake_client = FakeAnthropicClient(responses=script())
        await _run(fake_client, engine_name)
        requests_by_engine[engine_name] = _normalize(fake_client.messages.calls)

    assert requests_by_engine["graph"] == requests_by_engine["loop"]
    assert len(requests_by_engine["loop"]) == 3
