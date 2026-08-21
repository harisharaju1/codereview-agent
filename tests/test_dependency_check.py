import httpx

from src.services import dependency_check
from src.services.dependency_check import check_dependency_versions

CONTENTS_ROOT_URL = "https://api.github.com/repos/owner/repo/contents"


def test_parse_requirements_txt_skips_unpinned_and_comments():
    content = "\n".join(
        [
            "requests==2.31.0",
            "flask>=2.0",  # unpinned — not an exact pin, must be skipped
            "# a full-line comment",
            "",
            "httpx==0.28.1  # inline comment after a real pin",
            "-e git+https://example.com/some/pkg.git",
        ]
    )

    result = dependency_check._parse_requirements_txt(content)

    assert result == [
        dependency_check._DeclaredDependency("requests", "2.31.0"),
        dependency_check._DeclaredDependency("httpx", "0.28.1"),
    ]


def test_parse_pyproject_toml_extracts_pinned_dependencies():
    content = """
[project]
dependencies = ["requests==2.31.0", "flask>=2.0"]
"""
    result = dependency_check._parse_pyproject_toml(content)

    assert result == [dependency_check._DeclaredDependency("requests", "2.31.0")]


def test_parse_package_json_handles_caret_and_tilde_and_skips_ranges():
    content = """
{
  "dependencies": {"lodash": "^4.17.21", "left-pad": "1.3.0"},
  "devDependencies": {"jest": "~29.0.0", "typescript": ">=5.0.0 <6.0.0"}
}
"""
    result = dependency_check._parse_package_json(content)

    assert dependency_check._DeclaredDependency("lodash", "4.17.21") in result
    assert dependency_check._DeclaredDependency("left-pad", "1.3.0") in result
    assert dependency_check._DeclaredDependency("jest", "29.0.0") in result
    # A real range (not a ^/~ prefix on one version) has no single
    # "declared version" to compare, so it must be skipped entirely.
    assert not any(dep.name == "typescript" for dep in result)


def test_parse_csproj_extracts_package_references():
    content = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
  </ItemGroup>
</Project>"""

    result = dependency_check._parse_csproj(content)

    assert result == [dependency_check._DeclaredDependency("Newtonsoft.Json", "13.0.3")]


def test_is_outdated_true_for_a_real_numeric_gap():
    assert dependency_check._is_outdated("2.30.0", "2.31.0") is True


def test_is_outdated_treats_padded_versions_as_equal():
    # (1, 2) vs (1, 2, 0) must NOT be treated as outdated purely because
    # one tuple is shorter — see _is_outdated's own comment.
    assert dependency_check._is_outdated("1.2", "1.2.0") is False


def test_is_outdated_falls_back_to_string_compare_for_non_numeric_versions():
    # Neither side parses as a clean integer tuple (a "-beta" suffix) —
    # this exercises the documented fallback, not real semver precedence.
    assert dependency_check._is_outdated("2.0.0-beta", "2.0.0") is True


async def test_check_dependency_versions_end_to_end(respx_mock):
    respx_mock.get(CONTENTS_ROOT_URL, params={"ref": "main"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "requirements.txt", "path": "requirements.txt", "type": "file"},
                {"name": "README.md", "path": "README.md", "type": "file"},
                {"name": "src", "path": "src", "type": "dir"},
            ],
        )
    )
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/requirements.txt",
        params={"ref": "main"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "path": "requirements.txt",
                "content": "cmVxdWVzdHM9PTIuMzAuMA==",  # base64("requests==2.30.0")
                "encoding": "base64",
            },
        )
    )
    respx_mock.get("https://pypi.org/pypi/requests/json").mock(
        return_value=httpx.Response(200, json={"info": {"version": "2.31.0"}})
    )

    findings = await check_dependency_versions(
        httpx.AsyncClient(), "installation-token", "owner", "repo", "main"
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.package_name == "requests"
    assert finding.ecosystem == "pypi"
    assert finding.declared_version == "2.30.0"
    assert finding.latest_version == "2.31.0"
    assert finding.is_outdated is True


async def test_check_dependency_versions_treats_registry_404_as_undetermined(respx_mock):
    respx_mock.get(CONTENTS_ROOT_URL, params={"ref": "main"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "requirements.txt", "path": "requirements.txt", "type": "file"}],
        )
    )
    respx_mock.get(
        "https://api.github.com/repos/owner/repo/contents/requirements.txt",
        params={"ref": "main"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "path": "requirements.txt",
                # base64("internal-only-pkg==1.0.0")
                "content": "aW50ZXJuYWwtb25seS1wa2c9PTEuMC4w",
                "encoding": "base64",
            },
        )
    )
    respx_mock.get("https://pypi.org/pypi/internal-only-pkg/json").mock(
        return_value=httpx.Response(404)
    )

    findings = await check_dependency_versions(
        httpx.AsyncClient(), "installation-token", "owner", "repo", "main"
    )

    assert len(findings) == 1
    # A registry 404 must produce "undetermined," never a guessed answer —
    # latest_version stays None and is_outdated stays False, not True by
    # some default.
    assert findings[0].latest_version is None
    assert findings[0].is_outdated is False
