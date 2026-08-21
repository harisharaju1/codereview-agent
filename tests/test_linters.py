import shutil

import pytest

from src.services.linters.dispatch import run_linter

# These tests invoke the REAL ruff, oxlint, and dotnet binaries — not
# mocked subprocess calls — deliberately, per this project's own plan
# ("run_linter's three language paths each get at least one test asserting
# a real, correctly-parsed finding from that language's actual tool output
# format"). The tradeoff being made here explicitly: these tests are
# slower (the dotnet one especially, ~1-2s) and require all three tools
# actually installed to run at all, in exchange for testing something a
# mocked subprocess never could — that this project's parsing code
# actually agrees with what each real tool really prints, not with
# whatever shape a test author assumed it would print.
pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None, reason="requires the .NET SDK to be installed locally"
)


async def test_run_linter_python_reports_a_real_ruff_finding():
    result = await run_linter("bad.py", "import os\n")

    assert result.supported is True
    assert any(finding.rule == "F401" for finding in result.findings)


async def test_run_linter_typescript_reports_a_real_oxlint_finding():
    content = "function unused() {\n  const x = 5;\n  return 1;\n}\n"

    result = await run_linter("bad.ts", content)

    assert result.supported is True
    assert any("no-unused-vars" in (finding.rule or "") for finding in result.findings)


async def test_run_linter_csharp_reports_a_real_dotnet_format_finding():
    # Deliberately malformed brace placement/spacing — the same category
    # of issue confirmed, before writing csharp_linter.py, to be caught by
    # plain `dotnet format` but NOT by `dotnet format analyzers` alone
    # (see csharp_linter.py's own comment on why that subcommand choice
    # changed from what the plan originally assumed).
    content = "class Program {\n    static void Main() {\n        int x=1;\n    }\n}\n"

    result = await run_linter("bad.cs", content)

    assert result.supported is True
    assert any(finding.rule == "WHITESPACE" for finding in result.findings)


async def test_run_linter_returns_unsupported_for_an_unknown_extension():
    result = await run_linter("data.csv", "a,b,c\n1,2,3\n")

    assert result.supported is False
    assert result.findings == []
