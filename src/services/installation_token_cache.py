from datetime import UTC, datetime, timedelta

import httpx

from src.config.settings import Settings
from src.schemas.github_app import InstallationAccessToken
from src.services import github_app_auth

# Refresh a bit before actual expiry, not exactly at it — otherwise a token
# that's valid when checked here could expire moments later, mid-request,
# purely due to the small gap between "we checked" and "GitHub receives the
# call." A safety margin trades a slightly shorter effective token lifetime
# for never handing out one that's about to die.
_REFRESH_MARGIN = timedelta(seconds=60)

# Module-level cache, same shape/reasoning as session_store.py's _sessions
# dict: shared for the process lifetime, wiped on restart, not shared across
# multiple processes. Keyed by installation_id since a single App can have
# many installations (one per account/org it's installed on).
_tokens: dict[int, InstallationAccessToken] = {}


def _is_fresh(token: InstallationAccessToken) -> bool:
    return datetime.now(UTC) < (token.expires_at - _REFRESH_MARGIN)


async def get_installation_token(
    client: httpx.AsyncClient, settings: Settings, installation_id: int
) -> str:
    cached = _tokens.get(installation_id)
    if cached is not None and _is_fresh(cached):
        return cached.token

    app_jwt = github_app_auth.build_app_jwt(settings)
    fresh = await github_app_auth.fetch_installation_token(client, app_jwt, installation_id)
    _tokens[installation_id] = fresh
    return fresh.token
