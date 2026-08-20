import httpx
import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import installation_token_cache


@pytest.fixture(autouse=True)
def _clear_token_cache():
    installation_token_cache._tokens.clear()
    yield
    installation_token_cache._tokens.clear()


def _authenticated_client() -> TestClient:
    client = TestClient(app, follow_redirects=False)
    # Reuse the real /callback endpoint to obtain a correctly signed
    # installation cookie, the same approach test_github_app_router.py
    # uses — this also doubles as an integration check that a cookie
    # minted by /callback is usable here, not just by /installations/current.
    client.get("/github-app/callback", params={"installation_id": 123, "setup_action": "install"})
    return client


def _mock_installation_token(respx_mock):
    respx_mock.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_test_token",
                "expires_at": "2999-01-01T00:00:00Z",
            },
        )
    )


def test_list_pull_requests_without_cookie_returns_401():
    with TestClient(app) as client:
        response = client.get("/github-app/repos/owner/repo/pulls")

    assert response.status_code == 401


def test_list_pull_requests_returns_open_prs(respx_mock):
    _mock_installation_token(respx_mock)
    respx_mock.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Add feature",
                    "user": {"login": "harisharaju1"},
                    "state": "open",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
        )
    )

    with _authenticated_client() as client:
        response = client.get("/github-app/repos/owner/repo/pulls")

    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "number": 42,
            "title": "Add feature",
            "user": {"login": "harisharaju1"},
            "state": "open",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]


def test_list_pull_requests_404_from_github_becomes_service_404(respx_mock):
    _mock_installation_token(respx_mock)
    respx_mock.get("https://api.github.com/repos/owner/missing-repo/pulls").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with _authenticated_client() as client:
        response = client.get("/github-app/repos/owner/missing-repo/pulls")

    assert response.status_code == 404


def test_get_pull_request_diff_returns_raw_text(respx_mock):
    _mock_installation_token(respx_mock)
    respx_mock.get("https://api.github.com/repos/owner/repo/pulls/42").mock(
        return_value=httpx.Response(200, text="diff --git a/file b/file\n")
    )

    with _authenticated_client() as client:
        response = client.get("/github-app/repos/owner/repo/pulls/42/diff")

    assert response.status_code == 200
    assert response.text == "diff --git a/file b/file\n"
    assert response.headers["content-type"].startswith("text/plain")


def test_get_pull_request_diff_404_from_github_becomes_service_404(respx_mock):
    _mock_installation_token(respx_mock)
    respx_mock.get("https://api.github.com/repos/owner/repo/pulls/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with _authenticated_client() as client:
        response = client.get("/github-app/repos/owner/repo/pulls/999/diff")

    assert response.status_code == 404
