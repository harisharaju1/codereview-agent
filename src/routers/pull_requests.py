import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.config.settings import Settings, get_settings
from src.dependencies.http_client import get_http_client
from src.dependencies.installation import get_current_installation_id
from src.schemas.pull_request import PullRequestSummary
from src.services import installation_token_cache, pull_requests

router = APIRouter(prefix="/github-app", tags=["pull-requests"])


# Summary: lists a repo's open PRs, scoped to the caller's installation.
# Exists as the first "actually do something with GitHub data" endpoint —
# everything before this (Days 1-2) was about getting authenticated at all.
@router.get("/repos/{owner}/{repo}/pulls")
async def list_pull_requests(
    owner: str,
    repo: str,
    installation_id: int = Depends(get_current_installation_id),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> list[PullRequestSummary]:
    installation_token = await installation_token_cache.get_installation_token(
        client, settings, installation_id
    )
    try:
        return await pull_requests.list_open_pull_requests(client, installation_token, owner, repo)
    except httpx.HTTPStatusError as exc:
        raise _translate_github_error(exc) from exc


# Summary: fetches one PR's raw diff text. Exists as the eventual input to
# Week 2's review logic — nothing reviews anything yet, this just proves the
# raw material can be fetched correctly, with real error handling.
@router.get("/repos/{owner}/{repo}/pulls/{number}/diff")
async def get_pull_request_diff(
    owner: str,
    repo: str,
    number: int,
    installation_id: int = Depends(get_current_installation_id),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> Response:
    installation_token = await installation_token_cache.get_installation_token(
        client, settings, installation_id
    )
    try:
        diff_text = await pull_requests.fetch_pull_request_diff(
            client, installation_token, owner, repo, number
        )
    except httpx.HTTPStatusError as exc:
        raise _translate_github_error(exc) from exc

    # Returning a plain Response (not a dict/model) is how a FastAPI route
    # opts out of the default "serialize my return value as JSON" behavior —
    # necessary here since the body is raw diff text, not a JSON payload.
    return Response(content=diff_text, media_type="text/plain")


# Summary: turns a failed GitHub call into this service's own HTTP error.
# Exists so both endpoints above share one place that decides "404 means
# not found/not visible" and "anything else means an upstream failure,"
# instead of duplicating that judgment call per route.
def _translate_github_error(exc: httpx.HTTPStatusError) -> HTTPException:
    # GitHub deliberately returns 404 for both "this repo doesn't exist" and
    # "this installation can't see this repo" — it never distinguishes the
    # two, to avoid leaking which private repos exist to callers who
    # shouldn't even know they're there. This service preserves that same
    # ambiguity rather than trying to tell the two cases apart.
    if exc.response.status_code == status.HTTP_404_NOT_FOUND:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Repository or pull request not found"
        )
    # Anything else (rate limiting, a transient GitHub 5xx) is a real
    # upstream failure, not a "this thing doesn't exist" case — surfaced
    # as a 502 rather than silently swallowed. Retrying it is Day 4's job.
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub API request failed"
    )
