import pytest
from pydantic import ValidationError

from src.schemas.review import FileContent, ReviewFinding


def test_review_finding_accepts_valid_data_without_a_line():
    # `line` being entirely absent (not just None-ish) is the specific
    # behavior worth locking in with a test — this is what makes
    # ReviewFinding usable for file-level or PR-level findings, not just
    # line-specific ones. See the field's own comment in schemas/review.py
    # for why that matters.
    finding = ReviewFinding(
        file="requirements.txt",
        category="dependency",
        severity="medium",
        summary="requests is pinned below a version with a known CVE",
    )

    assert finding.line is None


def test_review_finding_rejects_invalid_severity():
    with pytest.raises(ValidationError):
        ReviewFinding(
            file="src/main.py",
            line=10,
            category="style",
            severity="catastrophic",  # not one of the three allowed values
            summary="this should fail validation",
        )


def test_file_content_round_trips_real_fields():
    content = FileContent(path="src/main.py", content="print('hi')\n", encoding="utf-8")

    assert content.path == "src/main.py"
    assert content.content == "print('hi')\n"
    assert content.encoding == "utf-8"
