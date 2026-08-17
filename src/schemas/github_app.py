from datetime import datetime

from pydantic import BaseModel


class InstallationAccessToken(BaseModel):
    token: str
    # Pydantic parses an ISO-8601 string (the format GitHub sends) straight
    # into a real `datetime` object here — no manual `datetime.fromisoformat`
    # call needed, validation and parsing happen in the same step.
    expires_at: datetime


class InstallationAccount(BaseModel):
    login: str


class Installation(BaseModel):
    # GitHub's installation object is a nested shape:
    # {"id": 123, "account": {"login": "...", ...}, ...}. Modeling `account`
    # as its own nested BaseModel (rather than flattening it into a made-up
    # `account_login` field) means the validation actually matches what
    # GitHub sends — Pydantic validates nested models recursively, so
    # `Installation.model_validate(...)` checks both levels in one call.
    id: int
    account: InstallationAccount


class Repository(BaseModel):
    id: int
    full_name: str
    private: bool


class RepositoryListResponse(BaseModel):
    total_count: int
    repositories: list[Repository]
