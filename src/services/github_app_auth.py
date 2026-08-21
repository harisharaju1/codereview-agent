import time
from pathlib import Path

import httpx
import jwt

from src.config.settings import Settings
from src.schemas.github_app import Installation, InstallationAccessToken
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"

# GitHub caps App JWTs at 10 minutes; kept short deliberately (unlike the
# session cookie's 7-day lifetime) since this token exists only to mint
# installation tokens, never to authorize an actual API call to repo data.
_JWT_EXPIRY_SECONDS = 10 * 60
# Backdating `iat` by a small margin tolerates clock drift between this
# machine and GitHub's servers — without it, a slightly-fast local clock
# could produce a JWT GitHub sees as "issued in the future" and rejects.
_JWT_CLOCK_SKEW_SECONDS = 60


def build_app_jwt(settings: Settings) -> str:
    private_key = Path(settings.github_app_private_key_path).read_text()
    now = int(time.time())

    payload = {
        "iat": now - _JWT_CLOCK_SKEW_SECONDS,
        "exp": now + _JWT_EXPIRY_SECONDS,
        # `iss` (issuer) is how GitHub knows which App this JWT claims to be
        # — it verifies the signature against that App's registered public
        # key, which is why forging a valid JWT requires the private key,
        # not just knowledge of the App ID.
        "iss": settings.github_app_id,
    }
    # RS256 is asymmetric signing: we sign with the private key (which only
    # this server ever holds), and GitHub verifies using the public key it
    # already has on file for this App — unlike the itsdangerous cookies
    # elsewhere in this project, which use a single shared secret both to
    # sign and to verify.
    return jwt.encode(payload, private_key, algorithm="RS256")


async def fetch_installation_token(
    client: httpx.AsyncClient, app_jwt: str, installation_id: int
) -> InstallationAccessToken:
    response = await call_with_retry(
        lambda: client.post(
            f"{API_BASE}/app/installations/{installation_id}/access_tokens",
            # Authenticated as the App itself (the JWT), not as an
            # installation — this call is what *produces* an installation
            # token in the first place, so it can't use one yet.
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    return InstallationAccessToken.model_validate(response.json())


async def fetch_installation(
    client: httpx.AsyncClient, app_jwt: str, installation_id: int
) -> Installation:
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    return Installation.model_validate(response.json())
