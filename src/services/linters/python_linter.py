import asyncio
import json
import tempfile
from pathlib import Path

from src.schemas.lint import LintFinding

# WHY THIS WRITES TO A REAL TEMP FILE, RATHER THAN USING ruff's OWN
# --stdin-filename SUPPORT (which genuinely exists — `ruff check
# --stdin-filename=x.py -` works fine on its own):
# the other two linters this package wraps can't take that shortcut —
# dotnet format (csharp_linter.py) needs a real project on disk to run
# against at all, and the installed oxlint version (js_ts_linter.py) has
# no stdin flag whatsoever. Giving all three linters the same "write
# content to a temp file, point the tool at it, clean up" shape means
# dispatch.py's dispatcher doesn't need to special-case how each language's
# tool actually receives its input — worth the one extra filesystem write
# this costs, for that consistency across all three.


# Summary: runs ruff against one file's content and returns typed findings.
# Exists as the Python arm of run_linter — the only one of the three
# language linters this project already depended on before Week 2 (as a
# dev-only tool for linting this project's OWN code); it's now also a
# runtime dependency, since the app itself invokes it against arbitrary
# reviewed code in production, not just during local development.
async def run_ruff(path: str, content: str) -> list[LintFinding]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Using the file's REAL basename (not a fixed placeholder like
        # "input.py") matters: ruff has filename-sensitive rules (e.g.
        # flagging a module that shadows a stdlib module name) that a fake
        # filename would silently make meaningless.
        file_path = Path(tmp_dir) / Path(path).name
        file_path.write_text(content)

        process = await asyncio.create_subprocess_exec(
            "ruff",
            "check",
            "--output-format=json",
            # --isolated stops ruff from walking UP the directory tree
            # looking for a pyproject.toml/ruff.toml to apply. Without it,
            # ruff could pick up THIS project's own lint config (line
            # length 100, this project's specific rule selection) and
            # silently apply it to someone else's reviewed code — the
            # rules that make sense for this codebase have no business
            # being imposed on an arbitrary external repo being reviewed.
            "--isolated",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # asyncio.create_subprocess_exec + .communicate(), not
        # subprocess.run() — this project is async throughout (every
        # GitHub call already goes through an async httpx client); a
        # blocking subprocess.run() call here would stall FastAPI's whole
        # event loop for the duration of the lint, blocking every other
        # concurrent request this process is handling, not just this one.
        stdout, _stderr = await process.communicate()

        # ruff exits non-zero whenever it finds ANY issue — that's
        # expected, normal behavior, not a failed invocation. Only the
        # JSON printed to stdout is meaningful here; if ruff itself failed
        # to run at all (missing binary, a crash), stdout would be empty
        # or non-JSON, and json.loads below raises rather than silently
        # reporting "zero findings" for what was actually a broken call.
        raw_findings = json.loads(stdout)

    return [
        LintFinding(
            file=path,
            line=item["location"]["row"],
            column=item["location"]["column"],
            rule=item["code"],
            message=item["message"],
            severity=item["severity"],
        )
        for item in raw_findings
    ]
