import anthropic
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
    client.get("/github-app/callback", params={"installation_id": 123, "setup_action": "install"})
    return client


def test_review_pull_request_without_cookie_returns_401():
    with TestClient(app) as client:
        response = client.post("/github-app/repos/owner/repo/pulls/1/review")

    assert response.status_code == 401


def test_review_pull_request_translates_anthropic_api_error_to_502(respx_mock, monkeypatch):
    respx_mock.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_test_token",
                "expires_at": "2999-01-01T00:00:00Z",
            },
        )
    )

    async def _raise_connection_error(*args, **kwargs):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    # run_review_agent is imported by name into src.routers.review, so the
    # patch target is the name as it's used there, not where it's defined
    # — the standard "patch where it's looked up" rule.
    monkeypatch.setattr("src.routers.review.run_review_agent", _raise_connection_error)

    with _authenticated_client() as client:
        response = client.post("/github-app/repos/owner/repo/pulls/1/review")

    assert response.status_code == 502
    assert response.json()["detail"] == "Claude API request failed"
