import re
from dataclasses import dataclass

from src.schemas.review import ReviewFinding

# WHAT THIS MODULE IS: the "one person merges the specialists' reports"
# step — deterministic, no LLM. Pure functions, so every rule here is
# testable without a graph, a model, or a network.
#
# WHY NO LLM SYNTHESIS PASS (merging near-duplicates worded differently,
# writing an overall summary, re-ranking by judgment): it would be one
# more call with the largest input of the whole run (every finding), and
# nondeterministic. Deterministic now; Day 3 measures how often
# differently-worded duplicates actually occur, and an LLM merge becomes a
# data-backed decision rather than a guess.

# A 200-finding review is noise — and from Week 4 these get posted to
# GitHub. Highest severity survives the cap first.
MAX_FINDINGS = 50

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _normalized_category(category: str) -> str:
    # "SQL Injection", "sql-injection" and "sql_injection" are the same
    # category for dedupe purposes.
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


@dataclass(frozen=True)
class Synthesis:
    findings: list[ReviewFinding]
    incomplete_specialists: list[str]
    dropped_findings: int
    all_failed: bool


# Summary: merges every specialist's result into one ranked, de-duplicated
# findings list, and reports which specialists didn't produce a result.
#
# Dedupe key: (file, line, normalized category). Two specialists flagging
# the same category on the same line are reporting one issue: keep the
# highest-severity version and record both as `sources`.
#
# WHY CATEGORY IS PART OF THE KEY (rather than merging everything on the
# same file+line): a performance issue and a security issue can genuinely
# sit on the same line, and merging them would silently drop one summary.
# This key never loses information; the cost is that the same issue
# described under two different categories survives as two findings. Day 3
# measures how often that happens.
def synthesize(specialist_results: list[dict], max_findings: int = MAX_FINDINGS) -> Synthesis:
    incomplete = sorted(r["name"] for r in specialist_results if r["status"] != "ok")
    all_failed = bool(specialist_results) and len(incomplete) == len(specialist_results)

    merged: dict[tuple, ReviewFinding] = {}
    for result in specialist_results:
        if result["status"] != "ok":
            continue
        for raw in result["findings"]:
            # sources are overwritten here, never trusted from the model —
            # see ReviewFinding.sources.
            finding = ReviewFinding.model_validate({**raw, "sources": [result["name"]]})
            key = (finding.file, finding.line, _normalized_category(finding.category))
            existing = merged.get(key)
            if existing is None:
                merged[key] = finding
                continue
            sources = sorted(set(existing.sources) | set(finding.sources))
            keep = (
                finding
                if _SEVERITY_RANK[finding.severity] < _SEVERITY_RANK[existing.severity]
                else existing
            )
            merged[key] = keep.model_copy(update={"sources": sources})

    ranked = sorted(
        merged.values(),
        key=lambda f: (
            _SEVERITY_RANK[f.severity],
            f.file,
            f.line if f.line is not None else 0,
        ),
    )
    return Synthesis(
        findings=ranked[:max_findings],
        incomplete_specialists=incomplete,
        dropped_findings=max(0, len(ranked) - max_findings),
        all_failed=all_failed,
    )
