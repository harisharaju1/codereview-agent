from src.services.review_graph.synthesize import synthesize


def _finding(file="a.py", line=1, category="bug", severity="low", summary="s"):
    return {
        "file": file,
        "line": line,
        "category": category,
        "severity": severity,
        "summary": summary,
    }


def _ok(name, *findings):
    return {"name": name, "status": "ok", "findings": list(findings)}


def _failed(name):
    return {"name": name, "status": "failed", "findings": [], "error": "x"}


def test_same_issue_from_two_specialists_merges_keeping_highest_severity():
    result = synthesize(
        [
            _ok("correctness", _finding(line=3, category="Injection", severity="medium")),
            _ok("security", _finding(line=3, category="injection", severity="high", summary="S")),
        ]
    )
    assert len(result.findings) == 1
    merged = result.findings[0]
    assert merged.severity == "high"
    assert merged.summary == "S"
    assert merged.sources == ["correctness", "security"]


def test_different_categories_on_the_same_line_are_both_kept():
    # A performance issue and a security issue can genuinely share a line;
    # merging them would silently drop one.
    result = synthesize(
        [
            _ok("performance", _finding(line=3, category="blocking-io")),
            _ok("security", _finding(line=3, category="injection")),
        ]
    )
    assert len(result.findings) == 2


def test_category_normalization_for_dedupe():
    result = synthesize(
        [
            _ok("a", _finding(category="SQL Injection")),
            _ok("b", _finding(category="sql_injection")),
        ]
    )
    assert len(result.findings) == 1


def test_findings_are_ranked_by_severity_then_file_then_line():
    result = synthesize(
        [
            _ok(
                "x",
                _finding(file="b.py", line=1, severity="low", category="c1"),
                _finding(file="a.py", line=9, severity="high", category="c2"),
                _finding(file="a.py", line=2, severity="high", category="c3"),
                _finding(file="a.py", line=None, severity="medium", category="c4"),
            )
        ]
    )
    assert [(f.severity, f.file, f.line) for f in result.findings] == [
        ("high", "a.py", 2),
        ("high", "a.py", 9),
        ("medium", "a.py", None),
        ("low", "b.py", 1),
    ]


def test_cap_keeps_the_most_severe_and_counts_the_rest():
    findings = [_finding(line=i, severity="low", category=f"c{i}") for i in range(5)]
    findings.append(_finding(line=99, severity="high", category="important"))
    result = synthesize([_ok("x", *findings)], max_findings=3)
    assert len(result.findings) == 3
    assert result.findings[0].severity == "high"
    assert result.dropped_findings == 3


def test_failed_specialists_are_reported_and_their_findings_ignored():
    result = synthesize([_ok("security", _finding()), _failed("performance")])
    assert result.incomplete_specialists == ["performance"]
    assert result.all_failed is False
    assert len(result.findings) == 1


def test_all_failed_only_when_every_started_specialist_failed():
    assert synthesize([_failed("a"), _failed("b")]).all_failed is True
    # No specialists at all (docs-only PR) is an empty review, not a failure.
    empty = synthesize([])
    assert empty.all_failed is False
    assert empty.findings == []


def test_sources_come_from_synthesis_not_from_the_model():
    result = synthesize([_ok("security", {**_finding(), "sources": ["made-up"]})])
    assert result.findings[0].sources == ["security"]
