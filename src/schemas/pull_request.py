from datetime import datetime

from pydantic import BaseModel, RootModel

from src.schemas.github_app import InstallationAccount


class PullRequestSummary(BaseModel):
    number: int
    title: str
    # Reusing InstallationAccount (a login-only account shape) rather than
    # inventing a near-duplicate model — GitHub's PR "user" object and its
    # installation "account" object are different endpoints returning the
    # same practical shape this project cares about (just a login).
    user: InstallationAccount
    state: str
    updated_at: datetime


# GitHub's PR-list endpoint returns a bare JSON array, not a wrapping object
# (unlike the installation-repositories endpoint, which wraps its list in
# {"total_count": ..., "repositories": [...]}). RootModel is Pydantic's way
# of validating "the whole response IS a list" rather than "the response HAS
# a list field" — model_validate() here checks/parses a raw list directly.
class PullRequestSummaryList(RootModel[list[PullRequestSummary]]):
    pass
