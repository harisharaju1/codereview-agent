import base64

import httpx
import pytest

from src.services.github_content import fetch_file_content

CONTENTS_URL = "https://api.github.com/repos/owner/repo/contents/src/main.py"


async def test_fetch_file_content_decodes_base64_response(respx_mock):
    # GitHub's real response wraps base64 content in surrounding newlines
    # every ~60 chars — deliberately reproduced here (not just a single
    # clean base64 blob) so this test actually exercises the "embedded
    # newlines don't break decoding" claim the function's own comment
    # makes, rather than just asserting something base64.b64decode would
    # have handled trivially either way.
    raw_text = "print('hello world')\n"
    encoded = base64.b64encode(raw_text.encode("utf-8")).decode("ascii")
    chunked = "\n".join(encoded[i : i + 20] for i in range(0, len(encoded), 20))

    respx_mock.get(CONTENTS_URL, params={"ref": "main"}).mock(
        return_value=httpx.Response(
            200,
            json={"path": "src/main.py", "content": chunked, "encoding": "base64"},
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_file_content(
            client, "installation-token", "owner", "repo", "src/main.py", "main"
        )

    assert result.path == "src/main.py"
    assert result.content == raw_text
    assert result.encoding == "utf-8"


async def test_fetch_file_content_404_propagates_as_http_status_error(respx_mock):
    # No router exists yet to translate this into a service-level 404 (that
    # translation, following Day 3's precedent, is the router layer's job,
    # not this plain service function's) — this test just confirms the
    # underlying failure surfaces as httpx's own exception rather than
    # being silently swallowed or misparsed.
    respx_mock.get(CONTENTS_URL, params={"ref": "main"}).mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await fetch_file_content(
                client, "installation-token", "owner", "repo", "src/main.py", "main"
            )

    assert exc_info.value.response.status_code == 404
