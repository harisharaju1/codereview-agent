import httpx

from src.schemas.pull_request import PullRequestDetail, PullRequestSummary, PullRequestSummaryList
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"


# Summary: fetches a single PR's metadata, in particular its head commit
# SHA. Exists because Day 4's agent loop needs a concrete ref to fetch
# files at — the PR's HEAD commit (the latest commit on the PR's branch),
# not `main`/`master` — and nothing built so far (list_open_pull_requests,
# fetch_pull_request_diff) returns that.
#
# WHY THIS IS A SEPARATE CALL FROM fetch_pull_request_diff, NOT ONE
# FUNCTION DOING BOTH:
# same URL, two different Accept headers, two different response shapes —
# exactly the content-negotiation pattern fetch_pull_request_diff already
# uses (default JSON here vs. the diff media type there). Merging them
# into one function would mean either always paying for both requests
# (wasteful when a caller only needs one) or a boolean flag changing what
# type the function returns, which is worse than two small, single-purpose
# functions.
async def fetch_pull_request_metadata(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str, number: int
) -> PullRequestDetail:
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    body = response.json()
    # Reaching into the nested "head": {"sha": ...} shape directly here,
    # rather than teaching PullRequestDetail GitHub's full nested object
    # for one field — see the schema's own comment for why.
    return PullRequestDetail(
        number=body["number"], title=body["title"], head_sha=body["head"]["sha"]
    )


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
