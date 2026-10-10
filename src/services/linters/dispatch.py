from pathlib import Path
from typing import Literal

from src.schemas.lint import LintResult
from src.services.linters.csharp_linter import run_dotnet_format
from src.services.linters.js_ts_linter import run_oxlint
from src.services.linters.python_linter import run_ruff

# WHY A DICT-BASED DISPATCH TABLE HERE, UNLIKE dependency_check.py's
# if/elif CHAIN FOR ITS OWN THREE-WAY MANIFEST DISPATCH:
# every branch here has the exact same shape — one file extension maps to
# exactly one async function taking (path, content) and returning
# list[LintFinding]. That uniformity is what makes a dict genuinely
# simpler than the alternative: adding a fourth language later is one new
# dict entry, nothing else changes. dependency_check.py's dispatch does
# NOT have this uniformity (each of its branches needs a different
# ecosystem label bundled alongside a different parser AND a different
# registry-lookup function — three things per branch, not one), which is
# exactly why that dispatch stayed a plain if/elif instead of a table.
# This is a concrete, side-by-side example of the same general judgment
# call (dict dispatch vs. if/elif) landing differently in two places in
# this same codebase, for a real, statable reason each time — not just
# stylistic inconsistency.
#
# WHY TWO TABLES (extension -> language, language -> linter) RATHER THAN
# THE ORIGINAL SINGLE extension -> linter TABLE: the review workflow's
# triage step (Week 3 Day 2) also needs "which language is this file?",
# independent of linting. Splitting the table makes the extension map the
# ONE place that answers that question for the whole codebase — triage
# and run_linter can never disagree about what a `.tsx` file is.
LintedLanguage = Literal["python", "js_ts", "csharp"]

_LANGUAGES_BY_EXTENSION: dict[str, LintedLanguage] = {
    ".py": "python",
    ".ts": "js_ts",
    ".tsx": "js_ts",
    ".js": "js_ts",
    ".jsx": "js_ts",
    ".cs": "csharp",
}

_LINTERS_BY_LANGUAGE = {
    "python": run_ruff,
    "js_ts": run_oxlint,
    "csharp": run_dotnet_format,
}


# Summary: the language this project has a real linter for, based on the
# file's extension — or None for anything else (Markdown, YAML, Go, ...).
# None means "no linter here", not "not code": a `.go` file is still code
# that reviewers should read, it just can't be run through run_linter.
def language_for_path(path: str) -> LintedLanguage | None:
    return _LANGUAGES_BY_EXTENSION.get(Path(path).suffix)


# Summary: the fourth and final tool — runs the correct real linter for a
# file based on its extension, behind one consistent interface regardless
# of which of the three underlying subprocess-based tools actually ran.
# Exists so every caller (eventually: the agent loop) has exactly one
# "lint this file" operation to reason about and call, never three
# separate ones it would otherwise have to choose between itself.
async def run_linter(path: str, content: str) -> LintResult:
    language = language_for_path(path)

    if language is None:
        # An unsupported extension is a clear, structured, honest result —
        # never a silently empty findings list (indistinguishable from
        # "checked, and it's clean") and never a raised exception either
        # (a language this tool doesn't cover yet isn't a failure of this
        # tool, it's a real, expected, sometimes-true outcome a caller
        # needs to be able to tell apart from an actual error).
        return LintResult(supported=False, findings=[])

    findings = await _LINTERS_BY_LANGUAGE[language](path, content)
    return LintResult(supported=True, findings=findings)
