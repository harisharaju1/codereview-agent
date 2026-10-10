import asyncio
import copy
import re
from collections import defaultdict

import anthropic
import httpx
import pytest

from src.config.settings import get_settings
from src.services.review_graph import specialists
from src.services.review_graph.workflow_graph import route_to_specialists, run_review_workflow
from src.services.review_tools import AllSpecialistsFailedError
from tests.test_review_agent import FakeMessage, FakeToolUseBlock

PR_URL = "https://api.github.com/repos/owner/repo/pulls/7"

# A PR touching one Python file, one root manifest, one doc and one
# lockfile — so a single fixture exercises every routing rule: three code
# specialists for src/app.py, the dependency specialist for
# requirements.txt, and nobody for README.md or uv.lock.
DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import sqlite3
-x = 1
+query = f"SELECT * FROM users WHERE name = '{name}'"
+cursor.execute(query)
diff --git a/requirements.txt b/requirements.txt
index 3333333..4444444 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1 +1 @@
-requests==2.32.0
+requests==2.20.0
diff --git a/README.md b/README.md
index 5555555..6666666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # app
+More docs.
diff --git a/uv.lock b/uv.lock
index 7777777..8888888 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-old
+new
"""

DOCS_ONLY_DIFF = """\
diff --git a/README.md b/README.md
index 5555555..6666666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # app
+More docs.
"""


class _RoutingFakeMessages:
    # WHY ROUTE BY CALLER INSTEAD OF BY CALL ORDER (Week 2's fake): the
    # workflow's specialists call the model CONCURRENTLY, so the order
    # their requests arrive in isn't deterministic. Each specialist's
    # system prompt starts "You are the <name> reviewer." — the fake reads
    # that and serves the next response from THAT specialist's own script.
    # A script entry can be a response, an exception to raise, or an async
    # callable (used to simulate a slow call).
    def __init__(self, scripts: dict[str, list]):
        self._scripts = {name: iter(items) for name, items in scripts.items()}
        self.calls: dict[str, list[dict]] = defaultdict(list)

    async def create(self, **kwargs):
        name = re.match(r"You are the (\w+) reviewer", kwargs["system"]).group(1)
        self.calls[name].append(copy.deepcopy(kwargs))
        item = next(self._scripts[name])
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


class RoutingFakeAnthropicClient:
    def __init__(self, scripts: dict[str, list]):
        self.messages = _RoutingFakeMessages(scripts)


def _submit(*findings: dict) -> FakeMessage:
    return FakeMessage(
        content=[FakeToolUseBlock(id="s", name="submit_review", input={"findings": list(findings)})]
    )


def _api_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def _mock_pr(respx_mock, diff: str = DIFF):
    # ORDER MATTERS: respx serves the FIRST registered route that matches,
    # and a bare PR_URL route also matches the diff request (same URL, it
    # just doesn't constrain the Accept header). The more specific diff
    # route must be registered first, or the diff request gets the JSON
    # metadata back and triage sees zero files.
    respx_mock.get(PR_URL, headers={"Accept": "application/vnd.github.v3.diff"}).mock(
        return_value=httpx.Response(200, text=diff)
    )
    respx_mock.get(PR_URL).mock(
        return_value=httpx.Response(
            200, json={"number": 7, "title": "Add user query", "head": {"sha": "abc123"}}
        )
    )


async def _run(fake_client):
    async with httpx.AsyncClient() as client:
        return await run_review_workflow(
            client, fake_client, get_settings(), "installation-token", "owner", "repo", 7
        )


INJECTION = {
    "file": "src/app.py",
    "line": 2,
    "category": "injection",
    "severity": "high",
    "summary": "SQL built from user input with an f-string.",
}


async def test_all_specialists_run_in_scope_and_results_merge(respx_mock):
    _mock_pr(respx_mock)
    fake = RoutingFakeAnthropicClient(
        {
            "security": [_submit(INJECTION)],
            # Same file, line and category as security's finding, lower
            # severity — must merge into ONE finding with both sources.
            "correctness": [_submit({**INJECTION, "severity": "medium", "summary": "Bug."})],
            "performance": [_submit()],
            "dependencies": [
                _submit(
                    {
                        "file": "requirements.txt",
                        "line": 1,
                        "category": "outdated-dependency",
                        "severity": "medium",
                        "summary": "requests downgraded to 2.20.0.",
                    }
                )
            ],
        }
    )

    result = await _run(fake)

    assert [(f.file, f.severity, f.sources) for f in result.findings] == [
        ("src/app.py", "high", ["correctness", "security"]),
        ("requirements.txt", "medium", ["dependencies"]),
    ]
    assert result.incomplete_specialists == []
    # Four specialists, one call each, FakeUsage's 100/20 per call.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (400, 80)
    assert set(result.usage_by_specialist) == {
        "security",
        "performance",
        "correctness",
        "dependencies",
    }
    assert result.usage_by_specialist["security"].input_tokens == 100

    # Context isolation: each specialist saw only ITS files' diffs, while
    # everyone saw the whole inventory (docs and lockfile included).
    security_message = fake.messages.calls["security"][0]["messages"][0]["content"]
    dependencies_message = fake.messages.calls["dependencies"][0]["messages"][0]["content"]
    assert "cursor.execute(query)" in security_message
    assert "requests==2.20.0" not in security_message
    assert "requests==2.20.0" in dependencies_message
    assert "cursor.execute(query)" not in dependencies_message
    for message in (security_message, dependencies_message):
        assert "uv.lock · modified · +1/-1 · not reviewed: lockfile" in message
        assert "More docs." not in message

    # Tool subsets: each specialist was only OFFERED its own tools.
    def offered(name):
        return {tool["name"] for tool in fake.messages.calls[name][0]["tools"]}

    assert offered("security") == {"get_file_content", "search_codebase", "submit_review"}
    assert offered("dependencies") == {"check_dependency_versions", "submit_review"}


async def test_one_failing_specialist_gives_a_partial_review(respx_mock):
    _mock_pr(respx_mock)
    fake = RoutingFakeAnthropicClient(
        {
            "security": [_submit(INJECTION)],
            "correctness": [_submit()],
            "performance": [_api_error()],
            "dependencies": [_submit()],
        }
    )

    result = await _run(fake)

    assert result.incomplete_specialists == ["performance"]
    assert [f.file for f in result.findings] == ["src/app.py"]
    # The failed specialist's spend is unknown (the API call raised), so
    # it's absent from the breakdown — the total is a lower bound.
    assert "performance" not in result.usage_by_specialist
    assert result.usage.input_tokens == 300


async def test_every_specialist_failing_is_a_failed_review(respx_mock):
    _mock_pr(respx_mock)
    fake = RoutingFakeAnthropicClient(
        {
            name: [_api_error()]
            for name in ("security", "correctness", "performance", "dependencies")
        }
    )

    with pytest.raises(AllSpecialistsFailedError) as exc_info:
        await _run(fake)

    assert "performance: Claude API error" in str(exc_info.value)
    assert exc_info.value.usage.input_tokens == 0


async def test_docs_only_pr_makes_zero_model_calls(respx_mock):
    _mock_pr(respx_mock, DOCS_ONLY_DIFF)
    fake = RoutingFakeAnthropicClient({})

    result = await _run(fake)

    assert result.findings == []
    assert result.usage.input_tokens == 0
    assert dict(fake.messages.calls) == {}


async def test_slow_specialist_times_out_without_sinking_the_review(respx_mock, monkeypatch):
    monkeypatch.setattr(specialists, "SPECIALIST_TIMEOUT_SECONDS", 0.2)
    _mock_pr(respx_mock)

    async def _hang():
        await asyncio.sleep(5)

    fake = RoutingFakeAnthropicClient(
        {
            "security": [_hang],
            "correctness": [_submit()],
            "performance": [_submit()],
            "dependencies": [_submit()],
        }
    )

    result = await _run(fake)

    assert result.incomplete_specialists == ["security"]
    assert "security" not in result.usage_by_specialist


async def test_specialist_that_never_submits_reports_its_spend(respx_mock):
    _mock_pr(respx_mock)
    fake = RoutingFakeAnthropicClient(
        {
            "security": [_submit()],
            "correctness": [_submit()],
            "performance": [FakeMessage(content=[], stop_reason="end_turn")],
            "dependencies": [_submit()],
        }
    )

    result = await _run(fake)

    # Unlike an API error, the call itself succeeded — its tokens are
    # known and counted even though the specialist failed.
    assert result.incomplete_specialists == ["performance"]
    assert result.usage_by_specialist["performance"].input_tokens == 100


def _state(files: list[dict]) -> dict:
    return {"pr_title": "t", "head_sha": "abc", "inventory": "", "files": files}


def _file(path: str, *, is_manifest=False, skip_reason=None) -> dict:
    return {
        "path": path,
        "previous_path": None,
        "status": "modified",
        "language": None,
        "is_manifest": is_manifest,
        "additions": 1,
        "deletions": 1,
        "diff_text": "",
        "truncated": False,
        "skip_reason": skip_reason,
    }


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ([_file("README.md", skip_reason="documentation")], "synthesize"),
        ([_file("uv.lock", skip_reason="lockfile")], "synthesize"),
        ([_file("src/app.py")], ["security", "performance", "correctness"]),
        (
            [_file("src/app.py"), _file("requirements.txt", is_manifest=True)],
            ["security", "performance", "correctness", "dependencies"],
        ),
        ([_file("requirements.txt", is_manifest=True)], ["dependencies"]),
    ],
)
def test_routing(files, expected):
    routed = route_to_specialists(_state(files))
    if isinstance(expected, str):
        assert routed == expected
    else:
        assert [send.arg["specialist"] for send in routed] == expected
