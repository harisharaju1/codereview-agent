import httpx

from src.services.code_search import search_codebase

SEARCH_URL = "https://api.github.com/search/code"


async def test_search_codebase_returns_matched_paths(respx_mock):
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "incomplete_results": False,
                "items": [{"path": "src/services/pull_requests.py", "sha": "abc123"}],
            },
        )
    )

    async with httpx.AsyncClient() as client:
        results = await search_codebase(
            client, "installation-token", "owner", "repo", "list_open_pull_requests"
        )

    assert len(results) == 1
    assert results[0].path == "src/services/pull_requests.py"


async def test_search_codebase_sends_the_repo_qualifier(respx_mock):
    # This is what actually proves the search is scoped to one repo rather
    # than GitHub's entire public index — asserting on the outbound
    # request's own query string, not just the (trivially mockable)
    # response.
    route = respx_mock.get(SEARCH_URL, params={"q": "TODO repo:owner/repo"}).mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "incomplete_results": False, "items": []}
        )
    )

    async with httpx.AsyncClient() as client:
        await search_codebase(client, "installation-token", "owner", "repo", "TODO")

    assert route.called
