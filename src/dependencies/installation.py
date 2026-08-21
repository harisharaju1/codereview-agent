from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config.settings import Settings, get_settings

# This module replaces dependencies/session.py from the OAuth App flow.
# The signed-cookie *mechanism* (itsdangerous, sign/verify) is identical —
# only what it signs changed, from an opaque per-user session id to an
# installation id. Unlike the OAuth session id, an installation id is
# meaningful on its own (it's what every GitHub API call gets scoped to),
# which is exactly why it's still worth signing: tampering with it isn't
# just "log in as someone else," it's "point this app's API calls at an
# installation that was never actually granted."
INSTALLATION_COOKIE_NAME = "installation_id"
INSTALLATION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days


# Summary: builds the itsdangerous signer used to sign and verify the
# installation_id cookie. A dedicated salt ("installation") keeps this
# signer's output distinct from any other cookie this project might sign
# with the same secret key.
def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret_key, salt="installation")


# Summary: turns a plain installation_id into a signed, tamper-evident
# string suitable for a cookie. Exists so /github-app/callback can hand the
# browser something it can carry around without being able to forge or
# alter which installation it points at.
def sign_installation_id(installation_id: int, settings: Settings) -> str:
    # itsdangerous signs strings, not ints — str(...) here, int(...) on the
    # way back out in get_current_installation_id below.
    return _serializer(settings).dumps(str(installation_id))


# Summary: the Depends()-injectable "current installation" check — reads,
# verifies, and unsigns the installation_id cookie, or raises 401. Exists as
# the one place every installation-scoped route asserts a valid installation
# rather than each route re-implementing the same cookie check.
def get_current_installation_id(
    request: Request, settings: Settings = Depends(get_settings)
) -> int:
    cookie_value = request.cookies.get(INSTALLATION_COOKIE_NAME)
    if cookie_value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No installation selected"
        )

    try:
        installation_id = _serializer(settings).loads(
            cookie_value, max_age=INSTALLATION_COOKIE_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid installation cookie"
        ) from exc

    return int(installation_id)
