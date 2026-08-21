import json
import re
import tomllib
import xml.etree.ElementTree as ET
from typing import NamedTuple

import httpx

from src.schemas.dependency_check import DependencyFinding
from src.services.github_content import fetch_file_content
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"

# MODULE-WIDE SCOPE LIMITATIONS — stated once, up front, rather than
# scattered across every function, since they all stem from the same
# underlying decision: this tool trades real coverage for a bounded,
# honestly-documented implementation rather than reaching for a full
# dependency-resolution library (pip's resolver, npm's, NuGet's) that
# would be far more correct but is out of scope for what a single day can
# responsibly build and actually understand end to end:
#
# 1. ONLY EXACT PINS ARE COMPARED. `requests==2.31.0` is checked;
#    `requests>=2.0`, `"^4.17.21"`, or a NuGet floating version isn't —
#    there's no single "declared version" to compare against latest when
#    a range is declared instead of a pin.
# 2. ONLY ROOT-LEVEL MANIFEST FILES ARE DISCOVERED. This tool lists the
#    repo's root directory once and looks for known manifest filenames
#    there — it does not recursively walk the whole tree (that would need
#    GitHub's Git Trees API with recursive=true, a heavier call this tool
#    doesn't make). A `.csproj` living in a subdirectory (the common case
#    for anything but a trivial solution) won't be found this way.
# 3. VERSION COMPARISON IS NOT REAL SEMVER. See _is_outdated below — this
#    compares dot-separated integer tuples, which handles the common case
#    correctly but doesn't implement real PEP 440 (Python) or semver.org
#    precedence rules (pre-release tags, build metadata, etc.).
#
# Each of these is a legitimate, bounded scope decision for a first
# implementation — not a bug to be quietly fixed later without deciding
# it's actually worth the added complexity.


class _DeclaredDependency(NamedTuple):
    # Deliberately NOT a Pydantic model, unlike DependencyFinding — this is
    # an internal intermediate value that never crosses a real boundary
    # (it's produced by parsing, consumed immediately by the version-check
    # loop, and never returned from this module or serialized anywhere).
    # The "typed boundaries around external data" principle this project
    # follows is about validating data AT a boundary, not about wrapping
    # every internal value in a schema regardless of whether anything
    # actually crosses in or out through it.
    name: str
    version: str


_PINNED_DEPENDENCY_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)")


def _parse_pinned_dependency(spec: str) -> _DeclaredDependency | None:
    # Shared by both requirements.txt (one spec per line) and
    # pyproject.toml's `project.dependencies` array (one spec per array
    # entry) — PEP 508 dependency specifier syntax is identical in both
    # places, so there's no reason to parse it twice with two regexes.
    match = _PINNED_DEPENDENCY_RE.match(spec)
    if match is None:
        return None
    return _DeclaredDependency(match.group(1), match.group(2))


def _parse_requirements_txt(content: str) -> list[_DeclaredDependency]:
    dependencies = []
    for line in content.splitlines():
        # Strip inline comments before parsing — `requests==2.31.0  # pinned
        # for CVE-1234` is valid requirements.txt and shouldn't fail to
        # parse just because a comment follows the pin on the same line.
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        dependency = _parse_pinned_dependency(line)
        if dependency is not None:
            dependencies.append(dependency)
        # Lines that don't match an exact pin (unpinned requirements,
        # `-e git+https://...` editable installs, environment markers) are
        # silently skipped — see the module-level "only exact pins" note.
    return dependencies


def _parse_pyproject_toml(content: str) -> list[_DeclaredDependency]:
    # tomllib is in the standard library as of Python 3.11 — no new
    # dependency needed for this, unlike JS/C#'s manifest formats which
    # this project has no built-in parser for at all (json and
    # xml.etree.ElementTree, used below, are also both stdlib).
    data = tomllib.loads(content)
    specs = data.get("project", {}).get("dependencies", [])
    dependencies = []
    for spec in specs:
        dependency = _parse_pinned_dependency(spec)
        if dependency is not None:
            dependencies.append(dependency)
    return dependencies


# Matches an optional leading `^` or `~` (npm's "compatible with" range
# prefixes) followed by a plain dotted-integer version, e.g. "^4.17.21" or
# "1.2.3" — but NOT genuine ranges like ">=1.0.0 <2.0.0" or "*", which this
# simple parser deliberately doesn't attempt to resolve to one version.
_NPM_VERSION_RE = re.compile(r"^[\^~]?(\d+(?:\.\d+)*)$")


def _parse_package_json(content: str) -> list[_DeclaredDependency]:
    data = json.loads(content)
    dependencies = []
    for section in ("dependencies", "devDependencies"):
        for name, raw_version in data.get(section, {}).items():
            match = _NPM_VERSION_RE.match(raw_version)
            if match is not None:
                dependencies.append(_DeclaredDependency(name, match.group(1)))
    return dependencies


def _parse_csproj(content: str) -> list[_DeclaredDependency]:
    root = ET.fromstring(content)
    dependencies = []
    # `.iter("PackageReference")` searches all descendants regardless of
    # nesting depth, rather than assuming a fixed structure — SDK-style
    # .csproj files vary in how ItemGroups are organized (some projects
    # split them across condition-guarded groups), but PackageReference
    # elements themselves are consistently shaped wherever they appear.
    for element in root.iter("PackageReference"):
        name = element.get("Include")
        version = element.get("Version")
        if name and version:
            dependencies.append(_DeclaredDependency(name, version))
        # The older, pre-SDK-style csproj format expresses the version as
        # a nested <Version> CHILD element instead of an attribute — not
        # handled here. Modern SDK-style projects (the default for
        # anything created with `dotnet new` since .NET Core) use the
        # attribute form this code reads.
    return dependencies


async def _fetch_latest_pypi_version(client: httpx.AsyncClient, name: str) -> str | None:
    try:
        response = await call_with_retry(lambda: client.get(f"https://pypi.org/pypi/{name}/json"))
    except httpx.HTTPStatusError:
        # A 404 here means "this package name doesn't exist on PyPI" —
        # plausible for a private/internal package pinned in
        # requirements.txt that was never meant to be public. Treated as
        # "couldn't determine latest," not a hard failure of the whole
        # tool — one unresolvable package shouldn't stop every other
        # finding in this PR from being reported.
        return None
    return response.json()["info"]["version"]


async def _fetch_latest_npm_version(client: httpx.AsyncClient, name: str) -> str | None:
    try:
        response = await call_with_retry(
            lambda: client.get(f"https://registry.npmjs.org/{name}/latest")
        )
    except httpx.HTTPStatusError:
        return None
    return response.json().get("version")


async def _fetch_latest_nuget_version(client: httpx.AsyncClient, name: str) -> str | None:
    try:
        # NuGet's flat-container index lists EVERY published version for a
        # package, oldest first — there's no dedicated "just give me
        # latest" endpoint the way PyPI and npm both have, so the latest
        # version is whichever one is last in this list, not a separate
        # field in the response.
        response = await call_with_retry(
            lambda: client.get(f"https://api.nuget.org/v3-flatcontainer/{name.lower()}/index.json")
        )
    except httpx.HTTPStatusError:
        return None
    versions = response.json().get("versions", [])
    return versions[-1] if versions else None


def _parse_numeric_tuple(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        # A version segment isn't a plain integer — e.g. "2.0.0-beta" or
        # "1.2.3-rc.1". Returning None here (rather than raising) is what
        # lets _is_outdated fall back to a weaker comparison instead of
        # this whole check crashing on a pre-release version string.
        return None


def _is_outdated(declared: str, latest: str) -> bool:
    declared_tuple = _parse_numeric_tuple(declared)
    latest_tuple = _parse_numeric_tuple(latest)

    if declared_tuple is not None and latest_tuple is not None:
        # Pad the shorter tuple with trailing zeros so "1.2" and "1.2.0"
        # compare as EQUAL rather than the shorter one always losing
        # (Python tuple comparison would otherwise treat (1, 2) as less
        # than (1, 2, 0) purely because it's shorter, which isn't the
        # semantic this project wants).
        length = max(len(declared_tuple), len(latest_tuple))
        declared_tuple = declared_tuple + (0,) * (length - len(declared_tuple))
        latest_tuple = latest_tuple + (0,) * (length - len(latest_tuple))
        return declared_tuple < latest_tuple

    # Fallback for anything _parse_numeric_tuple couldn't reduce to plain
    # integers. This is a real, documented limitation (see the module-level
    # comment's point 3, "not real semver") — "the strings differ" is a
    # conservative, sometimes-WRONG heuristic (e.g. "2.0.0" and "2.0.0-rc.1"
    # would be flagged as outdated in both directions depending on which is
    # "declared," even though a real semver-aware comparison might
    # correctly treat the release as newer than its own release candidate).
    # Accepted here rather than pulling in a real semver library, which
    # would need per-ecosystem rules anyway (PyPI/npm/NuGet don't all agree
    # on pre-release ordering).
    return declared != latest


_KNOWN_MANIFEST_NAMES = {"requirements.txt", "pyproject.toml", "package.json"}


async def _list_root_contents(
    client: httpx.AsyncClient, installation_token: str, owner: str, repo: str, ref: str
) -> list[dict]:
    # GitHub's Contents API, called with NO path (just the repo's contents
    # root) returns a directory LISTING (an array of entries) instead of a
    # single file's content — the same endpoint fetch_file_content calls,
    # behaving differently based on whether the target is a file or a
    # directory. This is GitHub's own API design, not a choice made here.
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/repos/{owner}/{repo}/contents",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    return response.json()


async def _check_manifest(
    client: httpx.AsyncClient, path: str, content: str
) -> list[DependencyFinding]:
    # A plain if/elif dispatch on the manifest's path, rather than a
    # dict-based dispatch table — considered and rejected specifically
    # because each branch needs a DIFFERENT ecosystem label alongside a
    # different parser and a different registry-lookup function, and a
    # dict of 3-tuples read worse than this does. Compare to run_linter's
    # dispatcher (Day 3), which genuinely does benefit from a dispatch
    # table since every branch there has the exact same shape (extension
    # -> one async function).
    if path == "requirements.txt":
        declared, ecosystem, fetch_latest = (
            _parse_requirements_txt(content),
            "pypi",
            _fetch_latest_pypi_version,
        )
    elif path == "pyproject.toml":
        declared, ecosystem, fetch_latest = (
            _parse_pyproject_toml(content),
            "pypi",
            _fetch_latest_pypi_version,
        )
    elif path == "package.json":
        declared, ecosystem, fetch_latest = (
            _parse_package_json(content),
            "npm",
            _fetch_latest_npm_version,
        )
    elif path.endswith(".csproj"):
        declared, ecosystem, fetch_latest = (
            _parse_csproj(content),
            "nuget",
            _fetch_latest_nuget_version,
        )
    else:
        return []

    findings = []
    for dependency in declared:
        latest = await fetch_latest(client, dependency.name)
        findings.append(
            DependencyFinding(
                file=path,
                ecosystem=ecosystem,
                package_name=dependency.name,
                declared_version=dependency.version,
                latest_version=latest,
                # latest is None means "couldn't determine" (registry
                # lookup failed/404) — is_outdated is unconditionally False
                # in that case, never guessed, since there's nothing to
                # compare against. See DependencyFinding's own docstring
                # comment for why None and "not outdated" are kept distinct.
                is_outdated=False if latest is None else _is_outdated(dependency.version, latest),
            )
        )
    return findings


# Summary: the third and fourth tools combined into one entry point —
# discovers which known manifest files exist at the repo's root for this
# ref, fetches each one's real content, parses out exact-pinned
# dependencies, and checks each against its ecosystem's public registry
# for a newer version. Exists to answer "did this PR touch a dependency
# that's now behind latest," across three different manifest formats and
# three different registries, behind one consistent typed result.
async def check_dependency_versions(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    ref: str,
) -> list[DependencyFinding]:
    root_entries = await _list_root_contents(client, installation_token, owner, repo, ref)
    manifest_paths = [
        entry["path"]
        for entry in root_entries
        if entry["type"] == "file"
        and (entry["name"] in _KNOWN_MANIFEST_NAMES or entry["name"].endswith(".csproj"))
    ]

    findings: list[DependencyFinding] = []
    for path in manifest_paths:
        # Reuses Day 1's get_file_content tool directly, rather than
        # duplicating a second "fetch a file" call here — this is a
        # concrete instance of one tool's implementation calling another
        # tool's implementation as a plain function, which is fine and
        # expected: only the AGENT LOOP (Day 4) treats these as
        # independent, LLM-selectable tools. Nothing stops one Python
        # function from calling another directly when it already knows
        # exactly which one it needs, same as any other internal call.
        file_content = await fetch_file_content(client, installation_token, owner, repo, path, ref)
        findings.extend(await _check_manifest(client, path, file_content.content))
    return findings
