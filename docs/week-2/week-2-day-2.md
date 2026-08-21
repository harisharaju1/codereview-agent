# Day 2 — `search_codebase` + `check_dependency_versions`

## What was built

- **`src/schemas/code_search.py`** — `CodeSearchResult` (`path`, `sha`), `CodeSearchResponse` (`total_count`, `incomplete_results`, `items`) — the subset of GitHub's code search response this project actually uses.
- **`src/services/code_search.py`** — `search_codebase`, the second tool: calls GitHub's code search API (`GET /search/code?q=...+repo:owner/repo`), scoped to one repo via the `repo:` search qualifier.
- **`src/schemas/dependency_check.py`** — `Ecosystem` (`Literal["pypi", "npm", "nuget"]`), `DependencyFinding` (`file`, `ecosystem`, `package_name`, `declared_version`, `latest_version: str | None`, `is_outdated`).
- **`src/services/dependency_check.py`** — `check_dependency_versions`, the third and fourth tools combined: lists a repo's root directory at a given ref, finds known manifest files (`requirements.txt`, `pyproject.toml`, `package.json`, any `*.csproj`), fetches each via Day 1's `fetch_file_content`, parses out exact-pinned dependencies, and checks each against its ecosystem's public registry (PyPI, npm, NuGet) for a newer version.
- **Tests**: `test_code_search.py`, `test_dependency_check.py` — 13 new tests, including direct unit tests of the pure parsing functions (no HTTP needed) and full end-to-end tests with mocked GitHub/registry responses.

## Why it's built this way

- **`search_codebase` calls GitHub's search API directly, not fetch-and-grep-locally.** Reuses existing auth, zero new infrastructure — at the cost of GitHub's search index occasionally lagging a very recent push. The alternative (documented, not built) is covered below.
- **`check_dependency_versions` only compares exact pins (`==`), never ranges.** A range like `requests>=2.0` or `"^4.17.21"` has no single "declared version" to compare against latest — flagging it as outdated (or not) would require picking an arbitrary interpretation of a range that isn't this tool's decision to make.
- **Manifest discovery covers the repo root only, not the full tree.** A real recursive search would need GitHub's Git Trees API with `recursive=true` — a heavier call this tool deliberately doesn't make yet. A `.csproj` in a subdirectory (the common case for anything beyond a trivial solution) won't be found this way.
- **Version comparison is dotted-integer-tuple comparison, not real semver.** PEP 440 (Python) and semver.org (npm/NuGet-adjacent) don't even agree with each other on pre-release ordering — implementing "real" comparison would mean picking per-ecosystem rules, which is a materially bigger scope than what a bounded first pass needs. The fallback (plain string inequality when either version doesn't parse as clean integers) is a documented, sometimes-wrong heuristic, not a correctness guarantee.

## Alternatives, Patterns, and Architecture Decisions

**Alternative considered and rejected for `search_codebase`: fetch relevant files via `get_file_content` and grep their content in this process.** This would sidestep GitHub's search-index lag entirely and give full control over matching — but it immediately raises a harder question this function never has to answer: *which* files to even fetch, on a repo that could have thousands of them. That "which files" problem is exactly what the large-diff preprocessing / inventory idea from Week 1's forward-looking notes would solve (a no-LLM pass that groups/filters a PR's changed files before anything expensive touches them) — worth building once a PR big enough to actually need it shows up in real testing, not speculatively here. This alternative is documented in `docs/week-2/week-2-plan.md` and referenced again here rather than silently dropped.

**Decision: `dependency_check.py`'s manifest dispatch is a plain `if/elif` chain, not a dict-based dispatch table.** This is worth contrasting directly with Day 3's `run_linter`, which *does* use a dict (see Day 3's retrospective). The difference: every branch in `run_linter`'s dispatch has the identical shape (extension → one function). Every branch in `check_dependency_versions`'s dispatch needs a *different* ecosystem label bundled with a *different* parser *and* a different registry-lookup function — three things per branch, not one — which reads worse as a dict of 3-tuples than as an `if/elif` chain naming each piece explicitly. Same general judgment call (dict vs. if/elif), landing differently in two places in the same codebase for a stated, real reason each time, not inconsistency.

**Decision: `_DeclaredDependency` (the parsed-but-not-yet-checked intermediate value) is a plain `NamedTuple`, not a Pydantic model — unlike every other typed value in this project.** This is a deliberate exception to the "typed boundaries around external data" principle, not a violation of it: that principle is about validating data *at a boundary* (something crossing into or out of this service). `_DeclaredDependency` never crosses a boundary — it's produced by parsing, consumed one function later by the version-check loop, and never serialized, returned, or logged anywhere. Wrapping it in a Pydantic model would add validation overhead for a value nothing external ever sees, purely for typing-everything's-own-sake. The real, external-facing output — `DependencyFinding` — is fully typed, which is where that discipline actually matters.

**Decision: `_parse_pinned_dependency`'s regex is shared between `requirements.txt` and `pyproject.toml` parsing.** Both use PEP 508 dependency specifier syntax for individual entries (`requirements.txt` one per line; `pyproject.toml`'s `project.dependencies` array one per array entry) — recognizing that they're the *same* micro-format, just embedded differently, avoided writing (and maintaining) two near-identical regexes that would inevitably drift apart over time.

**Decision: `latest_version: None` and `is_outdated: False` are kept semantically distinct from "up to date."** A registry lookup that 404s (private/internal package, typo, deleted package) sets `latest_version` to `None` — deliberately never guessed at, never defaulted to "not outdated" as if the check had actually run. This distinction is directly tested (`test_check_dependency_versions_treats_registry_404_as_undetermined`) specifically because it's the kind of thing that's easy to get subtly wrong (accidentally treating "couldn't check" the same as "checked, fine") without a test actively catching it.

## Python-specific things worth calling out

- **`tomllib`** (stdlib since Python 3.11) parses `pyproject.toml` with zero new dependencies — worth noting since this project has no built-in equivalent for `package.json` (plain `json`, also stdlib) or `.csproj` (`xml.etree.ElementTree`, also stdlib) needing anything extra either; all three manifest formats parse using only the standard library.
- **`xml.etree.ElementTree`'s `.iter("PackageReference")`** searches all descendants at any nesting depth, not just direct children — important because real `.csproj` files vary in how `ItemGroup`s are organized (some split `PackageReference`s across multiple condition-guarded groups), but the `PackageReference` elements themselves are consistently shaped wherever they sit in that structure.
- **`NamedTuple`, not a plain tuple or a dataclass**, for `_DeclaredDependency` — gives named-field access (`dependency.name`, `dependency.version`) with none of a full class's ceremony, appropriate for a small, immutable, internal-only value.

## .NET parallel

- GitHub's code search API, scoped via a `repo:` qualifier embedded in the query string itself (not a separate parameter) ≈ Azure DevOps' or GitHub Enterprise's own code search query syntax, where scoping qualifiers live inside one query string rather than as separate structured filter fields — a common API shape once a search index (not a database) is what's actually being queried.
- Three independent registry clients (PyPI, npm, NuGet) behind one typed result ≈ a `IPackageSource` abstraction in .NET tooling (NuGet itself supports multiple package sources behind one interface) — same "many backends, one contract" shape.
- `tomllib`/`json`/`ElementTree` all being stdlib, with no third-party parsing library needed for any of the three manifest formats ≈ System.Text.Json and System.Xml both being part of the BCL rather than NuGet packages — "the standard library already covers this" is a recognizable feeling in both ecosystems, just for a different specific set of formats.

## Verified manually

- `uv run pytest` (45 tests as of end of Day 3, 41 as of end of Day 2) and `uv run ruff check .` both clean.
- No manual/live verification against real GitHub or registry endpoints was performed this day — every test uses mocked responses (`respx`), consistent with every prior day's automated-first verification approach. A real end-to-end run against an actual PR (exercising all four tools together through the eventual agent loop) is Day 5's job, once there's a full loop to run.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Reusing Day 1's `fetch_file_content` from inside Day 2's `check_dependency_versions`** is the first time one of this week's tools calls another directly, as a plain function — a concrete demonstration that "these are independent, LLM-selectable tools" is a distinction that matters only at the agent-loop layer (Day 4), not at the plain-Python level, where nothing stops one tool's implementation from using another's.
- **The dict-vs-if/elif dispatch contrast with Day 3's `run_linter`** is a genuinely new kind of documentation for this project — previous weeks recorded *a* decision per pattern; this is the first time two structurally similar decisions in the same week are deliberately compared side by side to show why the same general choice went two different ways for two different, real reasons.
- **A registry-lookup-failure being modeled as "undetermined," not as a false negative,** continues the exact same discipline `github_retry.py` established for distinguishing "genuinely not found" from "couldn't check right now" — the same shape, now applied to a package registry instead of GitHub's API.
