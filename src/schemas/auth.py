from pydantic import BaseModel

# A Pydantic BaseModel subclass is doing two jobs at once here:
# 1. It's a type — `GitHubUser` is a real Python class other code can
#    reference in type hints (e.g. `-> GitHubUser`).
# 2. It's a runtime validator — `GitHubUser.model_validate(some_dict)` checks
#    that `some_dict` actually has the right shape (right field names, right
#    types) and raises a ValidationError if GitHub's API ever sends back
#    something unexpected. Plain dicts or dataclasses only give you one of
#    these two things, not both.


class GitHubTokenResponse(BaseModel):
    access_token: str
    token_type: str
    scope: str


class GitHubUser(BaseModel):
    login: str
    id: int
    # `str | None` is modern Python's "optional" type hint (equivalent to
    # `Optional[str]` from `typing`, just using the `|` union operator
    # directly — available since Python 3.10). `= None` gives the field a
    # default, so it's not required for a GitHubUser to be constructed.
    name: str | None = None
    avatar_url: str | None = None
