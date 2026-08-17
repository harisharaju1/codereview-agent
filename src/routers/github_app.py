import httpx
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import RedirectResponse

from src.config.settings import Settings, get_settings
from src.dependencies.http_client import get_http_client
from src.dependencies.installation import (
    INSTALLATION_COOKIE_MAX_AGE_SECONDS,
    INSTALLATION_COOKIE_NAME,
    get_current_installation_id,
    sign_installation_id,
)
from src.schemas.github_app import Installation, RepositoryListResponse
from src.services import github_app_auth, installation_token_cache

router = APIRouter(prefix="/github-app", tags=["github-app"])


@router.get("/install")
async def install(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    # No `state`/CSRF handling needed here, unlike the OAuth login redirect
    # this replaces — nothing sensitive is being exchanged by visiting
    # GitHub's own install page; the meaningful step is /callback below,
    # once GitHub tells us which installation was actually created.
    install_url = f"https://github.com/apps/{settings.github_app_slug}/installations/new"
    return RedirectResponse(url=install_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/callback")
async def callback(
    response: Response,
    installation_id: int,
    setup_action: str,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    # `setup_action` distinguishes a brand-new install from the user
    # updating an existing one's repo selection — both land here with a
    # valid installation_id, so this project doesn't need to branch on it
    # today, but it's worth knowing GitHub sends it.
    signed = sign_installation_id(installation_id, settings)
    response.set_cookie(
        INSTALLATION_COOKIE_NAME,
        signed,
        max_age=INSTALLATION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {"installation_id": str(installation_id), "setup_action": setup_action}


@router.get("/installations/current")
async def current_installation(
    installation_id: int = Depends(get_current_installation_id),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Installation | RepositoryListResponse]:
    app_jwt = github_app_auth.build_app_jwt(settings)
    installation = await github_app_auth.fetch_installation(client, app_jwt, installation_id)

    installation_token = await installation_token_cache.get_installation_token(
        client, settings, installation_id
    )
    repos_response = await client.get(
        "https://api.github.com/installation/repositories",
        headers={
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    repos_response.raise_for_status()
    repositories = RepositoryListResponse.model_validate(repos_response.json())

    return {"installation": installation, "repositories": repositories}
