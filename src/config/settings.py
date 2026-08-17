from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


# BaseSettings (from pydantic-settings) is Pydantic's BaseModel with one extra
# trick: on construction it reads values from environment variables (and,
# via model_config below, from a .env file) *automatically*, matching each
# field name case-insensitively. You never manually call os.environ.get(...)
# anywhere — the class declaration below IS the schema for what env vars
# this app needs, and Pydantic wires up the reading for you.
class Settings(BaseSettings):
    # SettingsConfigDict configures where BaseSettings looks for values.
    # env_file=".env" means: if a .env file exists in the working directory,
    # read it too (real process env vars still take priority over it).
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # `Literal["a", "b", "c"]` is a type hint meaning "must be exactly one of
    # these string values" — Pydantic turns this into a runtime check, not
    # just a static-analysis hint. Passing ENVIRONMENT=bogus raises a
    # ValidationError instead of silently being accepted as a string.
    environment: Literal["development", "test", "production"] = "development"

    # No `= default` here means these fields are *required* — constructing
    # Settings() without GITHUB_APP_ID etc. set in the environment raises
    # immediately. This is the "fail fast at startup" pattern: better to
    # crash on boot with a clear message than fail confusingly later on the
    # first real request that needs one of these.
    #
    # These replaced GITHUB_CLIENT_ID/GITHUB_CLIENT_SECRET/
    # GITHUB_OAUTH_REDIRECT_URI when this project moved from a GitHub OAuth
    # App to a GitHub App — see docs/week-1-day-2-github-app-plan.md. A
    # GitHub App authenticates as itself (a signed JWT, verified against a
    # private key) rather than as a logged-in user, so there's no client
    # secret or redirect URI in this model at all.
    github_app_id: str
    github_app_private_key_path: str
    # The App's URL-safe public name (visible in its settings page URL,
    # e.g. github.com/settings/apps/<this-slug>) — distinct from the numeric
    # App ID, and needed to build the public "install this app" page URL.
    github_app_slug: str
    session_secret_key: str


# @lru_cache is a decorator from the standard library that memoizes a
# function's return value based on its arguments — call get_settings() as
# many times as you want, the *first* call actually builds a Settings()
# instance (reading env vars), and every call after that just returns the
# same cached object instantly instead of re-reading the environment.
# This is also FastAPI's recommended pattern for a settings dependency:
# `Depends(get_settings)` then gives every route the same shared instance.
@lru_cache
def get_settings() -> Settings:
    return Settings()
