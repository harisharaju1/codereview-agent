import httpx

from src.config.settings import Settings
from src.schemas.auth import GitHubTokenResponse, GitHubUser

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


def build_authorize_url(settings: Settings, state: str) -> str:
    # httpx.QueryParams builds a correctly URL-encoded query string from a
    # dict — e.g. spaces or special characters in any value get percent-
    # encoded automatically. Hand-building "?client_id=" + x + "&..." with
    # plain string concatenation is exactly how query-encoding bugs happen.
    params = httpx.QueryParams(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_oauth_redirect_uri,
            # public_repo, not repo: this app only ever needs to read PRs/diffs,
            # and repo (the broader scope) grants full read/write on private
            # repos plus org-management access this app has no use for. See
            # docs/architecture-patterns.md's "Known limitation" note.
            "scope": "public_repo",
            "state": state,
        }
    )
    # An f-string (f"...{expr}...") evaluates `expr` and inserts it into the
    # string — Python's equivalent of a TS template literal (`...${expr}...`).
    return f"{AUTHORIZE_URL}?{params}"


# `async def` marks this as a coroutine function — calling it doesn't run
# the function immediately, it returns a coroutine object that has to be
# `await`ed (or scheduled by an event loop) to actually execute. Every
# `await` point below is a place this function can pause and let other
# coroutines run while waiting on network I/O, which is the entire point of
# async code: one thread can juggle many in-flight HTTP requests instead of
# blocking on each one in turn.
async def exchange_code_for_token(
    client: httpx.AsyncClient, settings: Settings, code: str
) -> GitHubTokenResponse:
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": settings.github_oauth_redirect_uri,
        },
        # GitHub's token endpoint defaults to a form-encoded response body;
        # this header is what makes it return JSON instead. Easy to miss —
        # skipping it doesn't error, it just silently gives you a different
        # (harder to parse) response format.
        headers={"Accept": "application/json"},
    )
    # Raises an httpx.HTTPStatusError if GitHub responded with a 4xx/5xx —
    # turns a "silently keep going with a bad response" bug into an
    # exception that surfaces immediately at the call site.
    response.raise_for_status()
    # model_validate(...) is Pydantic's "parse this dict/JSON into an
    # instance of this model, validating every field as you go" entry point.
    return GitHubTokenResponse.model_validate(response.json())


async def fetch_github_user(client: httpx.AsyncClient, access_token: str) -> GitHubUser:
    response = await client.get(
        USER_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    return GitHubUser.model_validate(response.json())
