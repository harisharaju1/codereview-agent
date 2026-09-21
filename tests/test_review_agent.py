from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from src.config.settings import get_settings
from src.services import review_agent

PR_URL = "https://api.github.com/repos/owner/repo/pulls/7"

# Fakes deliberately use plain dataclasses, not the real anthropic SDK
# types — review_agent.py only ever accesses `.type`/`.name`/`.id`/`.input`
# on a content block and `.content`/`.stop_reason` on a response, so
# duck-typed fakes covering exactly those attributes are sufficient and
# don't require constructing (and satisfying the validation of) real SDK
# objects just to test this project's own loop logic.


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeMessage:
    content: list
    stop_reason: str = "tool_use"


class FakeMessages:
    def __init__(self, responses: list[FakeMessage]):
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
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


async def _run(anthropic_client):
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        return await review_agent.run_review_agent(
            client, anthropic_client, settings, "installation-token", "owner", "repo", 7
        )


async def test_single_tool_call_then_submit_review(respx_mock):
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

    findings = await _run(fake_client)

    assert len(findings) == 1
    assert findings[0].file == "README.md"
    assert findings[0].severity == "low"
    assert len(fake_client.messages.calls) == 2


async def test_multiple_sequential_tool_calls_before_submit(respx_mock):
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

    findings = await _run(fake_client)

    assert findings == []
    assert len(fake_client.messages.calls) == 3


async def test_submit_review_self_corrects_after_invalid_input(respx_mock):
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

    findings = await _run(fake_client)

    assert len(findings) == 1
    assert findings[0].severity == "high"
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


async def test_exceeds_max_iterations_raises(respx_mock):
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
        for i in range(review_agent.MAX_ITERATIONS)
    ]
    fake_client = FakeAnthropicClient(responses=responses)

    with pytest.raises(review_agent.AgentExceededMaxIterationsError):
        await _run(fake_client)

    assert len(fake_client.messages.calls) == review_agent.MAX_ITERATIONS


async def test_agent_stopping_without_tool_use_raises():
    fake_client = FakeAnthropicClient(responses=[FakeMessage(content=[], stop_reason="end_turn")])

    import respx

    with respx.mock:
        _mock_pr_fetch(respx.mock)
        with pytest.raises(review_agent.AgentDidNotSubmitReviewError):
            await _run(fake_client)


async def test_failing_tool_executor_surfaces_as_error_tool_result(respx_mock):
    _mock_pr_fetch(respx_mock)
    # No respx route registered for get_file_content's contents call —
    # respx raises an assertion error for the unmocked request, which the
    # executor's try/except in run_review_agent must catch and convert
    # into an is_error tool_result rather than letting it crash the loop.
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

    findings = await _run(fake_client)

    assert findings == []
    second_call_messages = fake_client.messages.calls[1]["messages"]
    tool_result = next(
        block
        for msg in second_call_messages
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if block.get("type") == "tool_result" and block["tool_use_id"] == "t1"
    )
    assert tool_result["is_error"] is True
