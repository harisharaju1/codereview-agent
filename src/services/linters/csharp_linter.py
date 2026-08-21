import asyncio
import json
import tempfile
from pathlib import Path

from src.schemas.lint import LintFinding

# WHY THIS IS THE WEEK'S BIGGEST PIECE OF NEW INFRASTRUCTURE:
# there's no zero-config, single-file C# linter equivalent to ruff or
# oxlint — nothing that can just point at one .cs file with no surrounding
# project and produce diagnostics. The real fix used here: scaffold a
# throwaway, minimal .csproj around the single file, then run `dotnet
# format` against that scaffolded project and parse its structured report.
# This requires the .NET SDK installed wherever this code runs (a real,
# manual, machine-level setup step — this project's Docker image is NOT
# updated to include it this week, the same "don't build the
# container-level need before it's real" call already made for Docker on
# Day 5 of Week 1).
#
# WHY `dotnet format` (NO SUBCOMMAND), NOT `dotnet format analyzers`
# (WHAT WAS ORIGINALLY PLANNED):
# tested directly before writing this file: `dotnet format analyzers
# --verify-no-changes` and `dotnet format style --verify-no-changes` BOTH
# returned an empty report against a file with an obvious, real formatting
# problem (inconsistent brace placement, missing spaces around an
# operator) — those two subcommands only run analyzer- and style-rule
# diagnostics, and neither of those categories actually covers plain
# whitespace/formatting issues. `dotnet format whitespace
# --verify-no-changes` DID catch it. Rather than juggle three separate
# subcommands (and three separate result shapes) or silently miss the most
# common, simplest class of finding, this uses plain `dotnet format` with
# no subcommand at all — which runs whitespace, style, AND analyzer checks
# together, against one unified report. This is a concrete example of "the
# plan document's first guess at the exact command turned out to be wrong
# once actually tried" — worth stating explicitly, not quietly fixed
# without a trace, per this project's own standing practice of recording
# what didn't work and why.
_SCAFFOLD_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
"""


# Summary: runs `dotnet format` against one C# file's content, inside a
# throwaway scaffolded project, and returns typed findings. Exists as the
# C# arm of run_linter — the one language of the three that has no
# meaningful "just lint this one file" tool at all, unlike ruff and
# oxlint.
async def run_dotnet_format(path: str, content: str) -> list[LintFinding]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        # WHY THE SCAFFOLDED .csproj HAS NO <PackageReference> ITEMS AT
        # ALL, EVEN THOUGH A REAL PROJECT THIS FILE MIGHT HAVE COME FROM
        # almost certainly WOULD reference something:
        # keeping it dependency-free is what keeps `dotnet format`'s
        # implicit restore step fast — no NuGet packages to actually fetch
        # over the network, just the SDK's own already-locally-cached
        # implicit references. The real, accepted cost: analyzer
        # diagnostics that depend on resolving a symbol from a referenced
        # package (this scaffold has none) may misfire or simply not fire
        # at all for genuinely valid code. Whitespace and style
        # diagnostics (WHITESPACE, most IDE00xx rules) don't depend on
        # symbol resolution and are unaffected — which is realistically
        # most of what this tool is useful for on an isolated file anyway.
        (tmp_path / "review.csproj").write_text(_SCAFFOLD_CSPROJ)
        (tmp_path / Path(path).name).write_text(content)

        report_dir = tmp_path / "report"
        process = await asyncio.create_subprocess_exec(
            "dotnet",
            "format",
            # --verify-no-changes: report what WOULD change, change
            # nothing. This tool is reviewing someone else's code, not
            # rewriting it — the same "read, don't mutate" posture as
            # every other tool this project has built so far.
            "--verify-no-changes",
            "--report",
            str(report_dir),
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Like ruff, `dotnet format --verify-no-changes` exits non-zero
        # whenever it finds anything to report — expected, not a crash.
        # The return code is deliberately not checked here; whether
        # anything was found is read from the report file itself below,
        # which is a more direct signal than inferring it from an exit
        # code shared between "found issues" and other failure modes.
        await process.communicate()

        report_path = report_dir / "format-report.json"
        if not report_path.exists():
            # No report file at all — as opposed to a `[]` empty one, a
            # different and totally valid outcome meaning "clean" — is
            # what a genuinely broken invocation looks like (SDK missing,
            # scaffold failed to restore). Raising here, rather than
            # returning an empty findings list, keeps "we couldn't check
            # this" distinguishable from "we checked, it's clean" — the
            # exact same distinction LintResult.supported exists to
            # preserve one level up, in dispatch.py.
            raise RuntimeError("dotnet format produced no report — is the .NET SDK installed?")
        raw_report = json.loads(report_path.read_text())

    findings = []
    for document in raw_report:
        for change in document["FileChanges"]:
            findings.append(
                LintFinding(
                    file=path,
                    line=change["LineNumber"],
                    column=change["CharNumber"],
                    rule=change["DiagnosticId"],
                    message=change["FormatDescription"],
                    # dotnet format's own report has no severity gradient
                    # at all (unlike ruff/oxlint's error/warning split) —
                    # every entry just means "format would change this."
                    # "warning" here is a deliberate, documented default
                    # this project chose, not a real signal read from the
                    # tool's own output.
                    severity="warning",
                )
            )
    return findings
