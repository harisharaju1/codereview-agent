import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from src.config.settings import Settings, get_settings
from src.dependencies.http_client import get_http_client
from src.dependencies.session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    get_current_user,
    sign_session_id,
)
from src.schemas.auth import GitHubUser
from src.services import github_oauth, session_store

# APIRouter groups related routes and lets main.py mount them all under one
# prefix/tag in one line (app.include_router(auth.router)), instead of every
# route being registered individually on the app object directly.
router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_STATE_COOKIE_NAME = "oauth_state"
OAUTH_STATE_MAX_AGE_SECONDS = 60 * 10


@router.get("/github/login")
async def github_login(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    authorize_url = github_oauth.build_authorize_url(settings, state)

    response = RedirectResponse(url=authorize_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    # `state` here is deliberately just a random, unguessable value in an
    # unsigned cookie — it doesn't need itsdangerous's signature check the
    # way the session cookie does. Its only job is CSRF protection: prove
    # the browser completing /callback is the same one that started this
    # /login, which unpredictability alone is enough to guarantee.
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,  # this value is sent to GitHub(via queryparams in the auth URL) and
        # returned to us in /callback, so we can verify the same browser is completing the flow
        max_age=OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,  # not readable from JavaScript — blocks a class of XSS-based cookie theft
        samesite="lax",  # not sent on most cross-site requests — another CSRF mitigation layer
    )
    return response


@router.get("/github/callback", response_model=GitHubUser)
async def github_callback(
    request: Request,
    response: Response,
    # FastAPI reads `code` and `state` from the URL's query string
    # automatically because they're plain, un-annotated parameters that
    # aren't satisfied by a request body or a Depends(...) — this is
    # "declare the shape you want, FastAPI figures out where it comes from,"
    # the same idea Pydantic models apply to request bodies.
    code: str,
    state: str,
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> GitHubUser:
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    # secrets.compare_digest does a constant-time string comparison —
    # unlike `==`, it doesn't return faster when an early character already
    # differs, so it doesn't leak (via response-timing) how much of the
    # guess was correct. Standard practice any time you're comparing a
    # secret/security-sensitive value, even though a full timing attack over
    # HTTP is a fairly high bar to actually pull off.
    if cookie_state is None or not secrets.compare_digest(cookie_state, state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")

    response.delete_cookie(OAUTH_STATE_COOKIE_NAME)

    # `await`ing these one after another (not concurrently) is intentional:
    # fetch_github_user needs the access_token that exchange_code_for_token
    # produces, so the second call can't start until the first finishes —
    # a genuine sequential dependency, not a missed opportunity to
    # parallelize.
    token_response = await github_oauth.exchange_code_for_token(client, settings, code)
    user = await github_oauth.fetch_github_user(client, token_response.access_token)

    session_id = session_store.create_session(token_response.access_token, user)
    signed_session_id = sign_session_id(session_id, settings)

    response.set_cookie(
        SESSION_COOKIE_NAME,
        signed_session_id,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    # Declaring `response: Response` as a parameter and separately
    # `return`ing `user` is a FastAPI-specific pattern: it lets a route both
    # mutate response-level details (headers, cookies, status code — via
    # the injected Response object) and return a normal Python value for
    # the JSON body, which FastAPI serializes according to `response_model`.
    return user


@router.get("/me", response_model=GitHubUser)
async def me(user: GitHubUser = Depends(get_current_user)) -> GitHubUser:
    # This route handler has no logic of its own at all — the entire
    # "read the cookie, verify it, look up the session, or raise 401" job
    # is delegated to the get_current_user dependency. Any future route
    # that needs "the logged-in user, or a 401" (Day 3's PR endpoints, for
    # instance) just adds the same `Depends(get_current_user)` parameter —
    # the auth check isn't copy-pasted per route.
    return user
