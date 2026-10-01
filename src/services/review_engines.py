from collections.abc import Awaitable, Callable

import anthropic
import httpx

from src.config.settings import Settings
from src.schemas.review import Engine, ReviewResult
from src.services.review_agent import run_review_agent
from src.services.review_graph.loop_graph import run_review_graph

# Every engine is a function with this exact signature:
# (http client, anthropic client, settings, installation token, owner,
#  repo, PR number) -> ReviewResult, raising the shared review_tools
# exceptions on agent failure.
EngineFn = Callable[
    [httpx.AsyncClient, anthropic.AsyncAnthropic, Settings, str, str, str, int],
    Awaitable[ReviewResult],
]

# Summary: engine name -> the function that runs a review with it.
#
# WHY A PLAIN DICT: every engine deliberately shares one signature, so
# choosing an engine is a lookup, not a branch — adding Day 2's "workflow"
# or Day 3's "workflow_lc" is one line here and one value in the Engine
# Literal, with no router change. It's also the single seam the Day 3
# comparison harness uses to run every engine against the same PRs,
# in-process, without going through HTTP.
#
# WHY BOTH ENGINES STAY (rather than the new one replacing the old, as this
# project normally does with superseded code): comparing them is this
# week's deliverable. The Week 3 retrospective decides which engine
# survives into Week 4; the rest get deleted then, not left to rot.
ENGINES: dict[Engine, EngineFn] = {
    "loop": run_review_agent,
    "graph": run_review_graph,
}
