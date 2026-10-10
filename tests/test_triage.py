import pytest

from src.services.review_graph import triage
from src.services.review_graph.triage import render_inventory, triage_diff

# Triage is pure (diff text in, FileChange list out), so every rule is
# tested directly against small hand-written diffs — no network, no model.


def _modified(path: str, added_line: str = "+x = 2") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        f"{added_line}\n"
    )


BINARY = (
    "diff --git a/logo.png b/logo.png\n"
    "new file mode 100644\n"
    "index 0000000..abc1234\n"
    "Binary files /dev/null and b/logo.png differ\n"
)

DELETED = (
    "diff --git a/auth/middleware.py b/auth/middleware.py\n"
    "deleted file mode 100644\n"
    "index abc1234..0000000\n"
    "--- a/auth/middleware.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-check_auth()\n"
)

RENAMED = (
    "diff --git a/old_name.py b/new_name.py\n"
    "similarity index 90%\n"
    "rename from old_name.py\n"
    "rename to new_name.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/old_name.py\n"
    "+++ b/new_name.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)


def _only(diff: str):
    changes = triage_diff(diff)
    assert len(changes) == 1
    return changes[0]


@pytest.mark.parametrize(
    ("path", "expected_reason"),
    [
        ("uv.lock", "lockfile"),
        ("web/package-lock.json", "lockfile"),
        ("vendor/lib/thing.go", "vendored"),
        ("web/node_modules/pkg/index.js", "vendored"),
        ("static/app.min.js", "generated"),
        ("dist/bundle.js", "generated"),
        ("Forms/Main.Designer.cs", "generated"),
        ("docs/guide.md", "documentation"),
        ("README.rst", "documentation"),
        # .txt is deliberately NOT documentation — see triage.py.
        ("services/api/requirements.txt", None),
        ("notes.txt", None),
        ("src/app.py", None),
        ("main.go", None),
        (".github/workflows/ci.yml", None),
    ],
)
def test_skip_rules(path, expected_reason):
    assert _only(_modified(path)).skip_reason == expected_reason


def test_binary_file_is_skipped():
    change = _only(BINARY)
    assert change.skip_reason == "binary"
    assert change.diff_text == ""


def test_deleted_file_is_inventoried_but_skipped():
    change = _only(DELETED)
    assert change.status == "deleted"
    assert change.skip_reason == "deleted"
    # Still visible to every specialist through the inventory.
    assert "auth/middleware.py · deleted" in render_inventory([change])


def test_rename_records_previous_path():
    change = _only(RENAMED)
    assert change.status == "renamed"
    assert change.path == "new_name.py"
    assert change.previous_path == "old_name.py"
    assert change.skip_reason is None


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("a.tsx", "js_ts"),
        ("a.js", "js_ts"),
        ("a.cs", "csharp"),
        ("a.go", None),
        ("a.yml", None),
    ],
)
def test_language_comes_from_the_linter_extension_map(path, language):
    assert _only(_modified(path)).language == language


@pytest.mark.parametrize(
    ("path", "is_manifest"),
    [
        ("requirements.txt", True),
        ("pyproject.toml", True),
        ("package.json", True),
        ("App.csproj", True),
        # Nested manifests: check_dependency_versions can't see them, so
        # they must not schedule the dependency specialist.
        ("services/api/requirements.txt", False),
        ("src/App/App.csproj", False),
        ("src/app.py", False),
    ],
)
def test_manifest_detection_matches_what_the_dependency_tool_can_see(path, is_manifest):
    assert _only(_modified(path)).is_manifest is is_manifest


def test_large_file_diff_is_trimmed_and_marked(monkeypatch):
    monkeypatch.setattr(triage, "MAX_DIFF_LINES_PER_FILE", 10)
    added = "".join(f"+line {i}\n" for i in range(50))
    diff = (
        "diff --git a/big.py b/big.py\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/big.py\n"
        "@@ -0,0 +1,50 @@\n"
        f"{added}"
    )
    change = _only(diff)
    assert change.truncated is True
    assert change.additions == 50
    assert "diff truncated" in change.diff_text
    assert "get_file_content" in change.diff_text
    # 10 kept lines + the marker; nowhere near the original 56.
    assert change.diff_text.count("\n") < 15


def test_small_file_diff_is_kept_whole():
    change = _only(_modified("src/app.py"))
    assert change.truncated is False
    assert change.diff_text.startswith("diff --git a/src/app.py b/src/app.py")
    assert "+x = 2" in change.diff_text


def test_docs_only_pr_has_nothing_reviewable():
    changes = triage_diff(_modified("README.md") + _modified("docs/a.md"))
    assert [c.skip_reason for c in changes] == ["documentation", "documentation"]


def test_inventory_lists_every_file_with_its_status():
    changes = triage_diff(_modified("src/app.py") + _modified("uv.lock") + RENAMED)
    inventory = render_inventory(changes)
    assert inventory.splitlines() == [
        "- src/app.py · modified · python · +1/-1",
        "- uv.lock · modified · +1/-1 · not reviewed: lockfile",
        "- new_name.py · renamed · from old_name.py · python · +1/-1",
    ]
