from pathlib import Path

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
_LINTERS_BY_EXTENSION = {
    ".py": run_ruff,
    ".ts": run_oxlint,
    ".tsx": run_oxlint,
    ".js": run_oxlint,
    ".jsx": run_oxlint,
    ".cs": run_dotnet_format,
}


# Summary: the fourth and final tool — runs the correct real linter for a
# file based on its extension, behind one consistent interface regardless
# of which of the three underlying subprocess-based tools actually ran.
# Exists so every caller (eventually: the agent loop) has exactly one
# "lint this file" operation to reason about and call, never three
# separate ones it would otherwise have to choose between itself.
async def run_linter(path: str, content: str) -> LintResult:
    extension = Path(path).suffix
    linter = _LINTERS_BY_EXTENSION.get(extension)

    if linter is None:
        # An unsupported extension is a clear, structured, honest result —
        # never a silently empty findings list (indistinguishable from
        # "checked, and it's clean") and never a raised exception either
        # (a language this tool doesn't cover yet isn't a failure of this
        # tool, it's a real, expected, sometimes-true outcome a caller
        # needs to be able to tell apart from an actual error).
        return LintResult(supported=False, findings=[])

    findings = await linter(path, content)
    return LintResult(supported=True, findings=findings)
