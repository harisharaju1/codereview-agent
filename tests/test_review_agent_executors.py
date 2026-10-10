import json

import httpx

from src.services import review_tools

# These test the four executor functions directly, in isolation from the
# loop — proving the "agent-facing narrower shape correctly bridges to the
# real service function" claim for each tool individually, rather than
# only ever exercising them indirectly through a scripted loop scenario.


async def test_execute_get_file_content_returns_file_content_json(respx_mock):
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/a.py", params={"ref": "sha1"}
    ).mock(
        return_value=httpx.Response(
            200, json={"path": "a.py", "content": "cHJpbnQoMSk=", "encoding": "base64"}
        )
    )

    async with httpx.AsyncClient() as client:
        result = await review_tools._execute_get_file_content(
            client, "token", "owner", "repo", "sha1", {"path": "a.py"}
        )

    body = json.loads(result)
    assert body["path"] == "a.py"
    assert body["content"] == "print(1)"


async def test_execute_search_codebase_returns_json_list(respx_mock):
    respx_mock.get("https://api.github.com/search/code").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [{"path": "a.py", "sha": "deadbeef"}],
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await review_tools._execute_search_codebase(
            client, "token", "owner", "repo", "sha1", {"query": "foo"}
        )

    body = json.loads(result)
    assert body == [{"path": "a.py", "sha": "deadbeef"}]


async def test_execute_check_dependency_versions_returns_json_list(respx_mock):
    respx_mock.get("https://api.github.com/repos/owner/repo/contents", params={"ref": "sha1"}).mock(
        return_value=httpx.Response(200, json=[])
    )

    async with httpx.AsyncClient() as client:
        result = await review_tools._execute_check_dependency_versions(
            client, "token", "owner", "repo", "sha1", {}
        )

    assert json.loads(result) == []


async def test_execute_run_linter_fetches_content_then_lints(respx_mock):
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/bad.py", params={"ref": "sha1"}
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "path": "bad.py",
                # base64("import os\n")
                "content": "aW1wb3J0IG9zCg==",
                "encoding": "base64",
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await review_tools._execute_run_linter(
            client, "token", "owner", "repo", "sha1", {"path": "bad.py"}
        )

    body = json.loads(result)
    assert body["supported"] is True
    assert any(finding["rule"] == "F401" for finding in body["findings"])
