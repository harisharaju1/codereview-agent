import asyncio
import json
import tempfile
from pathlib import Path

from src.schemas.lint import LintFinding

# WHY oxlint, NOT ESLint (the far more commonly known JS/TS linter):
# ESLint's plugin/parser resolution genuinely assumes a real project
# context — a package.json, installed plugins, a resolvable config file —
# none of which exists for a single file's content fetched in isolation
# from an arbitrary external repo. oxlint is a zero-config, single-binary
# linter (a PyPI wheel bundling a native Rust binary, installed here via
# `uv add oxlint` — no Node.js or `npm install` required at all) that
# genuinely works against one file with no surrounding project. The real
# cost of this choice: oxlint implements a meaningful but not exhaustive
# subset of ESLint's rules, and doesn't apply any project-specific ESLint
# config a real repo might have — this tool reports what oxlint's own
# built-in rule set catches, not "everything that repo's actual ESLint
# setup would have caught."


# Summary: runs oxlint against one JS/TS file's content and returns typed
# findings. Exists as the TypeScript/JavaScript arm of run_linter.
async def run_oxlint(path: str, content: str) -> list[LintFinding]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = Path(tmp_dir) / Path(path).name
        file_path.write_text(content)

        process = await asyncio.create_subprocess_exec(
            "oxlint",
            str(file_path),
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        payload = json.loads(stdout)

    findings = []
    for diagnostic in payload["diagnostics"]:
        # oxlint's line/column info lives one level down, inside the
        # first "label" attached to a diagnostic (a diagnostic can have
        # zero labels for some rule types) — `labels` isn't guaranteed
        # non-empty, so this can't just always index [0] unconditionally.
        label = diagnostic["labels"][0] if diagnostic["labels"] else None
        span = label["span"] if label else None
        findings.append(
            LintFinding(
                file=path,
                line=span["line"] if span else None,
                column=span["column"] if span else None,
                rule=diagnostic.get("code"),
                message=diagnostic["message"],
                severity=diagnostic["severity"],
            )
        )
    return findings
