import httpx

from src.schemas.pull_request import PullRequestSummary, PullRequestSummaryList
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"


# Summary: fetches and validates a repo's open PRs from GitHub. Exists as
# the plain, HTTP-framework-agnostic function the router calls — no FastAPI
# code here, so it's callable/testable independent of any request.
async def list_open_pull_requests(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str
) -> list[PullRequestSummary]:
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 100},
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    # RootModel validation happens the same way as any other model's — the
    # difference is only in what the model wraps (a bare list vs. an object
    # with fields). `.root` unwraps back to a plain list[PullRequestSummary].
    return PullRequestSummaryList.model_validate(response.json()).root


# Summary: fetches one PR's raw diff text from GitHub, via content
# negotiation rather than a separate endpoint. Exists as the plain function
# behind the diff router — returns unstructured text on purpose, not a
# schema, since a diff has no shape worth validating.
async def fetch_pull_request_diff(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str, number: int
) -> str:
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {installation_token}",
                # This media type is a content-negotiation trick: same URL
                # as fetching the PR's normal JSON metadata, but this Accept
                # header asks GitHub for the raw unified diff text instead.
                # A diff is inherently unstructured text, not something
                # worth forcing into a Pydantic model, so this returns the
                # plain string as-is.
                "Accept": "application/vnd.github.v3.diff",
            },
        )
    )
    return response.text
