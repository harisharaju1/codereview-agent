from pydantic import BaseModel

# WHY THESE FIELDS AND NOT THE FULL SHAPE GITHUB SENDS BACK:
# GitHub's code search response includes a lot this project has no use for
# (a `repository` sub-object duplicating info already known from the
# request, a `score` relevance value, `html_url`, `git_url`, ...). Every
# schema in this project so far has modeled "the subset of an external
# response this project actually consumes," not "every field GitHub
# happens to send" — matching that discipline here rather than being
# exhaustive for its own sake.


class CodeSearchResult(BaseModel):
    path: str
    sha: str


class CodeSearchResponse(BaseModel):
    total_count: int
    # GitHub sets this true when its own search index couldn't complete a
    # full search in time (common right after a push, before indexing
    # catches up) — surfaced here, not silently dropped, specifically so a
    # caller (eventually: the agent, or a human reading logs) can tell
    # "these are all the matches" apart from "these are the matches GitHub
    # found before giving up," which is a meaningfully different claim.
    incomplete_results: bool
    items: list[CodeSearchResult]
