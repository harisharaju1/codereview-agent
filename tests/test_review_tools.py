import httpx

from src.services.review_tools import ToolCall, execute_tool_turn

# Direct tests of the shared per-turn tool execution for the two guards
# added on Week 3 Day 2. Both run before any executor, so no GitHub call
# is ever made for a rejected call (respx would fail the test if one were).


async def _turn(*calls: ToolCall, allowed_tools=None):
    async with httpx.AsyncClient() as client:
        return await execute_tool_turn(
            list(calls),
            client,
            "installation-token",
            "owner",
            "repo",
            "sha1",
            log_label="test",
            allowed_tools=allowed_tools,
        )


async def test_unknown_argument_is_rejected_with_a_corrective_message(respx_mock):
    # The exact call Haiku made on Day 1: `lines` isn't a parameter.
    turn = await _turn(
        ToolCall(id="t1", name="get_file_content", input={"path": "a.py", "lines": [72, 88]})
    )
    (result,) = turn.tool_results
    assert result["is_error"] is True
    assert "unknown argument(s): lines" in result["content"]
    assert "Accepted arguments: path" in result["content"]
    assert respx_mock.calls.call_count == 0


async def test_missing_required_argument_is_rejected(respx_mock):
    turn = await _turn(ToolCall(id="t1", name="search_codebase", input={}))
    (result,) = turn.tool_results
    assert result["is_error"] is True
    assert "missing required argument(s): query" in result["content"]


async def test_tool_outside_the_allowed_subset_is_rejected(respx_mock):
    turn = await _turn(
        ToolCall(id="t1", name="get_file_content", input={"path": "a.py"}),
        allowed_tools=frozenset({"check_dependency_versions"}),
    )
    (result,) = turn.tool_results
    assert result["is_error"] is True
    assert "not available to this reviewer" in result["content"]


async def test_submit_review_is_always_allowed_regardless_of_subset():
    turn = await _turn(
        ToolCall(id="t1", name="submit_review", input={"findings": []}),
        allowed_tools=frozenset({"check_dependency_versions"}),
    )
    assert turn.submitted_findings == []
