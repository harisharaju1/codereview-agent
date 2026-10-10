from typing import Literal

from pydantic import BaseModel

# WHY THIS FILE EXISTS BEFORE ANYTHING PRODUCES A ReviewFinding:
# every schema this project has written so far (schemas/github_app.py,
# schemas/pull_request.py) validates something GITHUB sent back — data
# flowing INTO this app from an external system. ReviewFinding is the
# first schema that describes something THIS APP is responsible for
# producing. Defining it now, days before Day 4's agent loop exists to
# fill it in, means every tool and every piece of agent-loop code written
# between now and then is written against the real, final contract — not
# a placeholder shape that gets reworked later once the "real" schema
# shows up. Getting the shape right early also matters more than usual
# here specifically because Week 3 (LangGraph) and Week 4 (MCP) both need
# to produce this exact same shape — a contract three different pieces of
# code have to agree on is worth nailing down once, deliberately.


class ReviewFinding(BaseModel):
    file: str
    # WHY OPTIONAL: not every finding is about one specific line. A
    # missing-dependency-version check, or "this file has no tests at
    # all," describes the whole file (or even the whole PR), not a line
    # within it. Making this required would force every tool that can't
    # point at a specific line to either lie (pick an arbitrary line
    # number) or be unable to report that finding at all — both worse
    # than just letting the field be absent when it doesn't apply.
    line: int | None = None
    # A free-text category rather than a Literal/enum of fixed categories
    # (e.g. "security", "performance", "style") is a deliberate choice, not
    # an oversight: the four tools this week (linters across three
    # languages, dependency checks, code search) will each surface
    # different kinds of issues, and Claude itself may describe a finding
    # in a way that doesn't map cleanly onto a small fixed set decided
    # ahead of time. Constraining `severity` below to a fixed set makes
    # sense because there are only ever three sane levels; constraining
    # `category` the same way would mean guessing every possible category
    # up front, which isn't a call this project is in a position to make
    # correctly yet.
    category: str
    severity: Literal["low", "medium", "high"]
    summary: str


class ReviewUsage(BaseModel):
    # WHY THIS IS TOKENS SUMMED ACROSS EVERY ITERATION, NOT JUST THE LAST
    # anthropic_client.messages.create() CALL:
    # a review can take several loop iterations (tool call, result, tool
    # call, ..., submit_review) — each one is a separate, separately
    # billed request. Only ever reporting the final call's usage would
    # silently undercount everything the earlier tool-calling turns cost.
    input_tokens: int
    output_tokens: int
    # None specifically means "no dollar cost applies or could be
    # computed" — not "this review was free." A caller-supplied/local
    # model has no real per-token price this project knows; an
    # unrecognized model id (the pricing table hasn't been updated for
    # it) is the same situation. Collapsing either case to 0.0 would be
    # actively misleading, the same reasoning DependencyFinding.latest_version
    # already uses None for "couldn't determine" rather than a guessed value.
    estimated_cost_usd: float | None


class ReviewResult(BaseModel):
    # WHY THIS WRAPS ReviewFinding RATHER THAN THE ENDPOINT RETURNING A
    # BARE list[ReviewFinding] (WHAT IT RETURNED BEFORE THIS WAS ADDED):
    # usage/cost describes the REVIEW RUN, not any individual finding —
    # attaching it to each ReviewFinding would duplicate the same numbers
    # across every entry (or force an awkward "put it on the first one"
    # convention). A wrapper is the natural place for a value that exists
    # exactly once per review.
    findings: list[ReviewFinding]
    usage: ReviewUsage


class FileContent(BaseModel):
    # WHY THIS EXISTS INSTEAD OF get_file_content JUST RETURNING `str`:
    # a bare string return type would work today, but it throws away two
    # things a caller might reasonably need later: which path this content
    # actually came from (useful once multiple files are fetched in one
    # agent turn and need to be told apart) and how it was encoded (GitHub
    # always sends base64 today, but pinning that assumption into every
    # caller instead of into one typed model is exactly the kind of
    # decision that's cheap to get right now and annoying to unwind later).
    path: str
    content: str
    encoding: str
