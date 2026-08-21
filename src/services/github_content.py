import base64

import httpx

from src.schemas.review import FileContent
from src.services.github_retry import call_with_retry

API_BASE = "https://api.github.com"


# Summary: fetches one file's real content from a repo at a given ref, via
# GitHub's Contents API. Exists as the first tool the eventual agent loop
# will call — this is what lets Claude actually read a file it decided it
# needs to look at, rather than reasoning only from the diff text it
# already has.
#
# WHY THIS TAKES installation_token: str, NOT installation_id: int:
# every other GitHub-calling function in this project (pull_requests.py's
# list_open_pull_requests, fetch_pull_request_diff) already takes a plain
# token string, not an id — the token-resolution step (check the cache,
# mint a fresh one if stale) happens exactly once, at the router layer,
# via installation_token_cache.get_installation_token(). If this function
# took an installation_id instead, it would either have to resolve the
# token itself (duplicating that logic in every tool function, once per
# tool, forever) or the caller would still have to resolve it first anyway
# and this parameter would just be redundant. Matching the existing
# signature shape is a deliberate consistency choice, not an accident.
#
# WHY THE DECODING HAPPENS HERE, NOT IN THE CALLER:
# GitHub's Contents API always returns file content base64-encoded,
# regardless of what the file actually contains — that's a transport-level
# detail of *this specific GitHub endpoint*, not something a caller of
# this function should need to know or handle themselves. Decoding inside
# this function means every caller (today: nothing yet; eventually: the
# agent loop's tool-dispatch code) gets back real, readable text directly.
# This is the same "encapsulate a transport detail inside the function that
# owns the HTTP call" instinct behind fetch_pull_request_diff's Accept
# header trick in pull_requests.py — the caller shouldn't have to know
# *how* GitHub represents the data over the wire, only what it means.
async def fetch_file_content(
    client: httpx.AsyncClient,
    installation_token: str,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> FileContent:
    response = await call_with_retry(
        lambda: client.get(
            f"{API_BASE}/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    )
    body = response.json()

    # GitHub's base64 payload is chunked with embedded newlines (to keep
    # individual lines from becoming unreasonably long in the raw JSON) —
    # base64.b64decode handles embedded whitespace/newlines transparently,
    # so no manual stripping is needed before decoding. `.decode("utf-8")`
    # then turns the resulting bytes into a real Python str; this assumes
    # the fetched file is UTF-8 text, which is a reasonable assumption for
    # source code specifically (the only kind of file this tool is ever
    # used to fetch) but would be the wrong call for a genuinely binary
    # file — not a case this tool needs to handle, since nothing in this
    # project's design ever asks it to fetch one.
    decoded_content = base64.b64decode(body["content"]).decode("utf-8")

    return FileContent(path=body["path"], content=decoded_content, encoding="utf-8")
