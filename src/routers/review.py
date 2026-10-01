import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.config.settings import Settings, get_settings
from src.dependencies.anthropic_client import get_anthropic_client
from src.dependencies.http_client import get_http_client
from src.dependencies.installation import get_current_installation_id
from src.schemas.review import Engine, ReviewResult
from src.services import installation_token_cache
from src.services.review_engines import ENGINES
from src.services.review_tools import (
    AgentDidNotSubmitReviewError,
    AgentExceededMaxIterationsError,
)

router = APIRouter(prefix="/github-app", tags=["review"])


# Summary: triggers a full agent-driven review of one PR and returns the
# structured findings alongside the token usage/estimated cost the review
# actually spent. Exists as the first endpoint in this project that
# actually produces a review rather than just fetching raw GitHub data —
# everything before this (Days 1-3) built the tools; this is what finally
# orchestrates them into something useful.
#
# WHY THIS IS SYNCHRONOUS (the request blocks until the review completes),
# NOT QUEUED: queueing long-running reviews behind SQS is explicitly
# Week 3's job, once that infrastructure exists — building it now would be
# solving a problem (many concurrent long-running reviews) this project
# doesn't have yet, the same "don't build the need before it's real"
# discipline already applied to Week 1's Docker/deploy decisions.
@router.post("/repos/{owner}/{repo}/pulls/{number}/review")
async def review_pull_request(
    owner: str,
    repo: str,
    number: int,
    installation_id: int = Depends(get_current_installation_id),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
    anthropic_client: anthropic.AsyncAnthropic = Depends(get_anthropic_client),
    # WHY A QUERY PARAMETER, NOT A SETTING: Week 3's comparison needs to
    # pick the engine PER REQUEST against the same running server — a
    # REVIEW_ENGINE env var would mean restarting between runs. Typed as the
    # Engine Literal, so an unknown name is FastAPI's automatic 422 with no
    # hand-written validation. Defaults to the Week 2 loop until Day 3's
    # measurements justify a different default.
    engine: Engine = Query("loop"),
) -> ReviewResult:
    installation_token = await installation_token_cache.get_installation_token(
        client, settings, installation_id
    )
    run_engine = ENGINES[engine]
    try:
        return await run_engine(
            client, anthropic_client, settings, installation_token, owner, repo, number
        )
    except (AgentDidNotSubmitReviewError, AgentExceededMaxIterationsError) as exc:
        # A different failure mode from Day 3's GitHub-404 translation —
        # the review genuinely failed to produce a result, an
        # agent/upstream failure rather than "this PR doesn't exist."
        # Day 3's _translate_github_error is untouched; this is new,
        # review-specific error handling, not a variant of it.
        #
        # WHY `detail` IS A DICT HERE, NOT JUST str(exc) LIKE EVERY OTHER
        # HTTPException IN THIS PROJECT: a failed review still spent real,
        # billed tokens getting to the point of failure — exc.usage (see
        # review_agent.py's own note on why both exceptions carry it) is
        # the only place that cost is recorded, since no ReviewResult ever
        # gets constructed on this path. FastAPI's HTTPException.detail
        # accepts any JSON-serializable value, not just a string, so this
        # doesn't need a parallel response schema — just a richer detail.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": str(exc), "usage": exc.usage.model_dump()},
        ) from exc
    except anthropic.APIError as exc:
        # Without this, an Anthropic failure that survives the SDK's own
        # internal retries (429/5xx retried automatically, up to
        # max_retries) would propagate all the way up as an unhandled
        # exception — FastAPI's default 500, with no clean translation,
        # unlike every GitHub failure this project already handles.
        # anthropic.APIError is the common base for both APIStatusError
        # (a real response, e.g. a bad/expired key) and
        # APIConnectionError (never reached Anthropic at all) — one catch
        # covers both, mirroring _translate_github_error's "anything else
        # is an upstream failure" branch. The exception's own message is
        # deliberately not echoed verbatim in the response (it can include
        # request/auth details not meant for an API caller); it's
        # preserved via `from exc` for whoever reads server-side logs.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Claude API request failed"
        ) from exc
