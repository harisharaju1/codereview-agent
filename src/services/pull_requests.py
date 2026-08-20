import httpx

from src.schemas.pull_request import PullRequestSummary, PullRequestSummaryList

API_BASE = "https://api.github.com"


async def list_open_pull_requests(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str
) -> list[PullRequestSummary]:
    response = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/pulls",
        params={"state": "open", "per_page": 100},
        headers={
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    response.raise_for_status()
    # RootModel validation happens the same way as any other model's — the
    # difference is only in what the model wraps (a bare list vs. an object
    # with fields). `.root` unwraps back to a plain list[PullRequestSummary].
    return PullRequestSummaryList.model_validate(response.json()).root


async def fetch_pull_request_diff(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str, number: int
) -> str:
    response = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {installation_token}",
            # This media type is a content-negotiation trick: same URL as
            # fetching the PR's normal JSON metadata, but this Accept header
            # asks GitHub for the raw unified diff text instead. A diff is
            # inherently unstructured text, not something worth forcing into
            # a Pydantic model, so this returns the plain string as-is.
            "Accept": "application/vnd.github.v3.diff",
        },
    )
    response.raise_for_status()
    return response.text
