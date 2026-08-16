from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config.settings import Settings, get_settings
from src.schemas.auth import GitHubUser
from src.services import session_store

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    # URLSafeTimedSerializer (from itsdangerous) signs a value with a secret
    # key so that any tampering is detectable, and embeds a timestamp so a
    # signed value can also be checked for age later (see max_age= below).
    # "Signed" is not the same as "encrypted": the session id itself is
    # still readable if someone gets the cookie — signing only proves it
    # wasn't forged or altered, it doesn't hide the content. That's exactly
    # why we only ever sign an opaque session id here, never the real
    # GitHub access token — see session_store.py's comment on why the real
    # secret always stays server-side.
    # A leading underscore in `_serializer` is Python's convention (not an
    # enforced rule — see the earlier conversation about this) for "this is
    # an internal helper for this module, not part of its public interface."
    return URLSafeTimedSerializer(settings.session_secret_key, salt="session")


def sign_session_id(session_id: str, settings: Settings) -> str:
    return _serializer(settings).dumps(session_id)


def get_current_user(request: Request, settings: Settings = Depends(get_settings)) -> GitHubUser:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        # .loads(...) is the inverse of .dumps(...) above — it verifies the
        # signature (raises BadSignature if it doesn't match, meaning the
        # cookie was tampered with or wasn't signed by us at all) and checks
        # the embedded timestamp against max_age (raises SignatureExpired if
        # it's too old), then returns the original session id string.
        session_id = _serializer(settings).loads(cookie_value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired) as exc:
        # `raise ... from exc` explicitly chains the new exception to the
        # one that caused it — Python then shows both in the traceback
        # ("the above exception was the direct cause of the following
        # exception"), which is far more useful for debugging than the new
        # HTTPException appearing to come from nowhere.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc

    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session not found")

    return session.user
