from typing import Literal

from pydantic import BaseModel

# WHY THIS ISN'T JUST A dict[str, str]:
# a raw dict of package-name -> version would be simpler to produce, but it
# throws away exactly the information a caller (eventually: the agent,
# deciding whether this is worth flagging) actually needs to reason about:
# which ecosystem this package belongs to (the same package name can exist
# independently in PyPI and npm), and whether a "latest" comparison was
# even possible at all. The Ecosystem/DependencyFinding split keeps that
# information typed instead of encoded into a comment or a dict key naming
# convention.

Ecosystem = Literal["pypi", "npm", "nuget"]


class DependencyFinding(BaseModel):
    file: str
    ecosystem: Ecosystem
    package_name: str
    declared_version: str
    # None specifically means "a version comparison wasn't attempted" —
    # NOT "this package has no newer version." Those are different claims:
    # the former covers unpinned requirements (`requests>=2.0`, where
    # "declared" isn't one exact version to compare) and registry lookups
    # that failed or returned nothing; collapsing that into "no update
    # needed" would be actively misleading, not just imprecise.
    latest_version: str | None
    is_outdated: bool
