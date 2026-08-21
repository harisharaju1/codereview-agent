from pydantic import BaseModel


class LintFinding(BaseModel):
    file: str
    line: int | None
    column: int | None
    # `rule` and `severity` are both plain strings, not fixed enums — three
    # unrelated underlying tools (ruff, oxlint, dotnet format) each have
    # their own rule-naming scheme (ruff: "F401"; oxlint:
    # "eslint(no-unused-vars)"; dotnet format: "WHITESPACE") and their own
    # severity vocabulary (dotnet format has no severity gradient at all —
    # see csharp_linter.py for how that's handled). Forcing a single shared
    # taxonomy across three tools that were never designed to agree with
    # each other would either lose real information or require an
    # arbitrary, made-up translation layer this project has no actual use
    # for. Mapping a LintFinding into ReviewFinding's stricter
    # three-value severity (if that ever happens) is Day 4's agent-loop
    # concern — this schema stays close to each tool's own raw output.
    rule: str | None
    message: str
    severity: str


class LintResult(BaseModel):
    # WHY THIS ISN'T JUST list[LintFinding], WITH AN EMPTY LIST MEANING
    # "UNSUPPORTED":
    # an empty list is genuinely ambiguous — it could mean "this file was
    # checked and is completely clean" (a real, good outcome) or "no
    # linter exists for this file's language" (a limitation of this tool,
    # not a fact about the code). Those are different claims a caller
    # needs to be able to tell apart; collapsing them into the same empty
    # list would silently misreport "we didn't check this" as "we checked
    # this and it's fine."
    supported: bool
    findings: list[LintFinding]
