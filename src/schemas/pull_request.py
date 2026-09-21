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


class PullRequestDetail(BaseModel):
    # WHY THIS MODEL EXISTS, SEPARATE FROM PullRequestSummary:
    # GitHub's "get one PR" endpoint returns a much larger object than the
    # list endpoint's per-item shape — this models only the one additional
    # field Day 4's agent loop actually needs (head.sha, to know which
    # commit to fetch files at), not an attempt at a fuller PR model.
    number: int
    title: str
    # GitHub nests this as {"head": {"sha": "...", ...}} — flattened here
    # to head_sha rather than a nested model, since nothing else in that
    # nested object is used. model_validate() reads the input by field
    # name, not by attribute path, so the flattening needs an explicit
    # field alias or a validator; simplest is fetch_pull_request_metadata
    # reaching into the JSON directly (see pull_requests.py) rather than
    # teaching this schema GitHub's full nested shape for one field.
    head_sha: str
