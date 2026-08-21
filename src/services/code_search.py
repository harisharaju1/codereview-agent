import httpx

from src.schemas.code_search import CodeSearchResponse, CodeSearchResult
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"


# Summary: searches a repo's indexed code for a query string, via GitHub's
# code search API. Exists as the second tool the agent loop will be able
# to call — where get_file_content answers "what's in this specific file,"
# this answers "which files even mention X at all," letting the agent
# widen its own investigation instead of being limited to files already
# named in the diff.
#
# WHY THIS CALLS GITHUB'S SEARCH API DIRECTLY, RATHER THAN FETCHING FILES
# AND GREPPING THEM IN THIS PROCESS (the alternative considered in
# docs/week-2/week-2-plan.md):
# reusing the exact same auth this project already has, with zero new
# infrastructure, was judged worth a real, known tradeoff: GitHub's search
# index can lag a few seconds to minutes behind a very recent push. The
# alternative (fetch relevant files via get_file_content, search their
# content here) avoids that lag entirely but immediately raises a harder
# question this function doesn't have to answer at all: *which* files to
# even fetch, on a repo that could have thousands of them. That "which
# files" problem is exactly what the large-diff preprocessing / inventory
# idea from Week 1's forward-looking notes would solve — worth building
# once a PR big enough to actually need it shows up, not speculatively
# here. Search-index lag is an accepted, documented limitation, not an
# oversight.
async def search_codebase(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    query: str,
) -> list[CodeSearchResult]:
    # GitHub's code search syntax combines a free-text query with
    # space-separated qualifiers in the SAME `q` parameter (there's no
    # separate "repo" field to fill in) — `repo:owner/repo` is one such
    # qualifier, scoping the search to just this repository instead of
    # GitHub's entire public code index. httpx handles the URL-encoding of
    # the space between the query text and the qualifier automatically via
    # the `params` dict; this isn't something that needs to be done by hand.
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/search/code",
            params={"q": f"{query} repo:{owner}/{repo}"},
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    return CodeSearchResponse.model_validate(response.json()).items
