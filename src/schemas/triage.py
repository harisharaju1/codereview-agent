from typing import Literal

from pydantic import BaseModel

SkipReason = Literal["deleted", "binary", "lockfile", "vendored", "generated", "documentation"]


# Summary: one changed file in a pull request, as the review workflow's
# triage step understands it — what happened to it, what kind of file it
# is, whether a reviewer should read it, and its diff (possibly trimmed).
#
# WHY THIS EXISTS AS A SCHEMA: it's the contract between triage (which
# produces it, deterministically, with no LLM) and everything after it —
# routing (which specialists to start), each specialist's first message,
# and the inventory every specialist sees. It lives in graph state as a
# dumped dict (see review_graph/state.py on plain data in state), and is
# re-validated wherever it's consumed.
class FileChange(BaseModel):
    path: str
    # Set only for renames: the path the file had before this PR.
    previous_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed"]
    # The linted language (python / js_ts / csharp), or None for any other
    # file — see linters/dispatch.py's language_for_path. None does NOT
    # mean "not code"; it means "no linter for it".
    language: str | None
    # True only for a ROOT-level manifest that check_dependency_versions
    # can actually see — see dependency_check.is_root_manifest_path.
    is_manifest: bool
    additions: int
    deletions: int
    # This file's slice of the PR diff, capped at a fixed number of lines
    # (see triage.MAX_DIFF_LINES_PER_FILE). Empty for skipped files — no
    # reviewer reads them, so there's no reason to carry their text around
    # (or, from Day 4, to checkpoint it).
    diff_text: str
    truncated: bool
    # None means "a reviewer should read this file". Anything else is why
    # it was set aside — the file still appears in every specialist's
    # inventory, so a reviewer can see that e.g. `uv.lock` changed or
    # `auth/middleware.py` was deleted and investigate if it matters.
    skip_reason: SkipReason | None
