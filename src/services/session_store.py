import secrets
from dataclasses import dataclass

from src.schemas.auth import GitHubUser


# @dataclass auto-generates the boilerplate a plain class would otherwise
# need by hand: __init__ (assigns github_access_token/user from constructor
# args), __repr__ (a readable string representation for debugging/logging),
# and __eq__ (so two SessionData instances with equal fields compare equal).
# Unlike GitHubUser above, this class is never built from external/untrusted
# data (only from values this code already produced), so there's no need for
# Pydantic's validation here — a dataclass is the lighter-weight tool for an
# internal, trusted data holder.
@dataclass
class SessionData:
    github_access_token: str
    user: GitHubUser


# A module-level dict — created once, when this module is first imported,
# and shared by every function below for the lifetime of the process. This
# is Python's version of a "static"/singleton in-memory store: there's no
# class instance holding this, it just lives at module scope. It's also why
# this doesn't survive a process restart, and won't be shared across
# multiple server processes — a known, deliberate limitation for Week 1.
_sessions: dict[str, SessionData] = {}


def create_session(access_token: str, user: GitHubUser) -> str:
    # secrets.token_urlsafe(32) generates a cryptographically secure random
    # string (from the `secrets` module, specifically designed for security
    # tokens — unlike the general-purpose `random` module, which is not safe
    # for anything like session IDs, since it's predictable if you know its
    # internal state).
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = SessionData(github_access_token=access_token, user=user)
    return session_id


def get_session(session_id: str) -> SessionData | None:
    # dict.get(key) returns None if the key isn't present, instead of
    # raising a KeyError the way _sessions[session_id] would — exactly what
    # we want here, since "no session with this id" is an expected, normal
    # outcome (an expired/invalid cookie), not an exceptional one.
    return _sessions.get(session_id)
