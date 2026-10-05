from pathlib import PurePosixPath

from unidiff import PatchedFile, PatchSet

from src.schemas.triage import FileChange, SkipReason
from src.services.dependency_check import is_root_manifest_path
from src.services.linters.dispatch import language_for_path

# WHAT THIS MODULE IS: the "sort the mail before handing it out" step of
# the review workflow. It turns a PR's raw unified diff into one
# FileChange per file — what happened to it, what kind of file it is,
# whether a reviewer should read it, and a size-capped slice of its diff.
#
# WHY THERE IS NO LLM HERE: every rule below is mechanical ("is this a
# lockfile?", "is this binary?", "did a manifest change?"). A model-based
# router would cost a call on every review (including trivial ones), and
# be nondeterministic — the same PR could get different reviewers on
# different runs, which would make Day 3's engine comparison noisy. An LLM
# router earns its place only when routing needs JUDGMENT; that would slot
# in later as one more node, after this one.
#
# WHY IT EXISTS AT ALL — measured, not assumed: on Day 1, the large PR's
# diff was 397 KB, of which ~375 KB was Markdown docs, and the Week 2 loop
# re-sent the whole thing on every iteration (~114k input tokens per call,
# $0.35 on Haiku). The code in that PR was ~75 KB. This module is what
# stops reviewers paying to read docs and lockfiles.
#
# WHY unidiff RATHER THAN A HAND-WRITTEN PARSER: unified-diff edge cases
# (renames with similarity index, "\ No newline at end of file", binary
# markers, mode-only changes, paths with spaces) are a known bug farm, and
# diff parsing isn't this week's learning goal. This project hand-rolls the
# mechanic being STUDIED (the agent loop in Week 2, retries in Week 1), not
# everything.

# Big enough that ordinary files arrive whole; small enough that one huge
# generated-looking file can't dominate a specialist's first message. A
# truncated file isn't hidden — the specialist is told, and can still call
# get_file_content for the full text.
MAX_DIFF_LINES_PER_FILE = 400

_LOCKFILE_NAMES = {
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "packages.lock.json",
    "Cargo.lock",
    "go.sum",
}

# Matched against any directory segment in the path, so `vendor/x.go` and
# `web/node_modules/y.js` are both caught.
_VENDORED_DIRECTORIES = {"vendor", "node_modules", "third_party"}
_GENERATED_DIRECTORIES = {"dist", "build"}
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".g.cs", ".designer.cs", "_pb2.py", ".pb.go")

# Prose, not code. Routed away from the code specialists: on Day 1 the
# large PR's Markdown was ~95% of its diff, and the top "finding" was the
# model reporting the content of old docs as a defect. Docs review is a
# real need, but a different reviewer with a different brief — not a
# security or performance specialist reading prose.
#
# WHY `.txt` IS NOT IN THIS LIST: too many `.txt` files are not prose —
# requirements.txt (root or nested), CMakeLists.txt, robots.txt. Treating
# a stray notes.txt as code costs a little reading; treating a dependency
# file as documentation silently loses real review coverage. The cheap
# mistake is the one to allow.
_DOCUMENTATION_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")


def _skip_reason(patched_file: PatchedFile, path: str) -> SkipReason | None:
    # Order matters: the FIRST matching reason is the one reported, from
    # most to least fundamental. A deleted lockfile is "deleted" (nothing
    # to read at all) before it's "lockfile".
    pure_path = PurePosixPath(path)
    lowered_name = pure_path.name.lower()
    directories = set(pure_path.parts[:-1])

    if patched_file.is_removed_file:
        return "deleted"
    if patched_file.is_binary_file:
        return "binary"
    if pure_path.name in _LOCKFILE_NAMES:
        return "lockfile"
    if directories & _VENDORED_DIRECTORIES:
        return "vendored"
    if directories & _GENERATED_DIRECTORIES or lowered_name.endswith(_GENERATED_SUFFIXES):
        return "generated"
    if lowered_name.endswith(_DOCUMENTATION_SUFFIXES):
        return "documentation"
    return None


def _status(patched_file: PatchedFile) -> str:
    if patched_file.is_removed_file:
        return "deleted"
    if patched_file.is_added_file:
        return "added"
    if patched_file.is_rename:
        return "renamed"
    return "modified"


def _strip_diff_prefix(path: str) -> str:
    # unidiff's source_file/target_file keep git's "a/" and "b/" prefixes.
    return path[2:] if path.startswith(("a/", "b/")) else path


def _trimmed_diff(patched_file: PatchedFile) -> tuple[str, bool]:
    lines = str(patched_file).splitlines(keepends=True)
    if len(lines) <= MAX_DIFF_LINES_PER_FILE:
        return "".join(lines), False
    omitted = len(lines) - MAX_DIFF_LINES_PER_FILE
    marker = (
        f"\n[... diff truncated: {omitted} more lines not shown. "
        f"Call get_file_content for the full file if you need it ...]\n"
    )
    return "".join(lines[:MAX_DIFF_LINES_PER_FILE]) + marker, True


# Summary: parses a PR's unified diff into one FileChange per changed file,
# in diff order. Deterministic and free: same diff in, same result out.
def triage_diff(diff_text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for patched_file in PatchSet(diff_text):
        path = patched_file.path
        skip_reason = _skip_reason(patched_file, path)
        diff_slice, truncated = ("", False) if skip_reason else _trimmed_diff(patched_file)
        changes.append(
            FileChange(
                path=path,
                previous_path=(
                    _strip_diff_prefix(patched_file.source_file) if patched_file.is_rename else None
                ),
                status=_status(patched_file),
                language=language_for_path(path),
                is_manifest=is_root_manifest_path(path),
                additions=patched_file.added,
                deletions=patched_file.removed,
                diff_text=diff_slice,
                truncated=truncated,
                skip_reason=skip_reason,
            )
        )
    return changes


# Summary: the one-line-per-file overview every specialist receives, for
# EVERY changed file (skipped ones included, with the reason).
#
# WHY EVERY SPECIALIST GETS ALL OF IT, when each only receives the diffs
# of its own files: this is the shared, compact GLOBAL context ("what is
# the shape of this whole PR?") that pairs with each specialist's
# detailed LOCAL context (its own diffs). A security reviewer seeing that
# `auth/middleware.py` was deleted, or that a migration was added, can go
# and look — for the cost of one short line per file.
def render_inventory(changes: list[FileChange]) -> str:
    lines = []
    for change in changes:
        parts = [change.path, change.status]
        if change.previous_path:
            parts.append(f"from {change.previous_path}")
        if change.language:
            parts.append(change.language)
        parts.append(f"+{change.additions}/-{change.deletions}")
        if change.truncated:
            parts.append("diff truncated")
        if change.skip_reason:
            parts.append(f"not reviewed: {change.skip_reason}")
        lines.append("- " + " · ".join(parts))
    return "\n".join(lines)
