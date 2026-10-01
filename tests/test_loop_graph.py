import json

import httpx
from anthropic.types import TextBlock

from src.config.settings import get_settings
from src.services.review_graph.loop_graph import LOOP_GRAPH, RECURSION_LIMIT
from src.services.review_graph.state import ReviewContext
from src.services.review_tools import MAX_ITERATIONS
from tests.test_review_agent import FakeAnthropicClient, FakeMessage, FakeToolUseBlock

# Graph-specific checks that the parametrized parity tests in
# test_review_agent.py can't express, because they only see each engine's
# final ReviewResult/exception — not the graph's structure or its state.


def test_loop_graph_has_exactly_the_ported_nodes():
    # The faithful port is two nodes and nothing else: call_model and
    # run_tools (plus LangGraph's implicit START/END). An extra node would
    # mean the port does something the Week 2 loop doesn't.
    nodes = set(LOOP_GRAPH.get_graph().nodes)
    assert nodes == {"__start__", "call_model", "run_tools", "__end__"}


def test_recursion_limit_leaves_room_for_the_project_iteration_cap():
    # Each iteration is two super-steps (call_model + run_tools). If the
    # framework's cap were at or below that, LangGraph's own
    # GraphRecursionError would fire before the project's named
    # AgentExceededMaxIterationsError (with its attached usage) could.
    assert RECURSION_LIMIT > 2 * MAX_ITERATIONS


async def test_graph_state_holds_only_json_serializable_data(respx_mock):
    # Guards the Day 1 decision that state never holds Anthropic SDK
    # objects: a checkpointer (Day 4) persists exactly this state, and SDK
    # objects in it trip a "will be blocked in a future version"
    # deserialization warning. If json.dumps can serialize every message,
    # nothing SDK-shaped slipped in. A TextBlock alongside the tool call
    # covers both block types the model returns.
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
                    TextBlock(type="text", text="Let me read the README first."),
                    FakeToolUseBlock(id="t1", name="get_file_content", input={"path": "README.md"}),
                ]
            ),
            FakeMessage(
                content=[FakeToolUseBlock(id="t2", name="submit_review", input={"findings": []})]
            ),
        ]
    )

    async with httpx.AsyncClient() as client:
        final_state = await LOOP_GRAPH.ainvoke(
            {
                "messages": [{"role": "user", "content": "diff"}],
                "iteration": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "stop_reason": None,
                "findings": None,
                "outcome": None,
            },
            config={"recursion_limit": RECURSION_LIMIT},
            context=ReviewContext(
                http_client=client,
                anthropic_client=fake_client,
                settings=get_settings(),
                installation_token="installation-token",
                owner="owner",
                repo="repo",
                head_sha="abc123",
                pr_number=7,
            ),
        )

    assert final_state["outcome"] == "submitted"
    json.dumps(final_state["messages"])
    json.dumps(final_state["findings"])
    assistant_blocks = final_state["messages"][1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "Let me read the README first."}
    # Credentials live only in runtime context, never in state.
    assert "installation-token" not in json.dumps(final_state)
