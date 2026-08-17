from datetime import UTC, datetime, timedelta

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


def test_install_redirects_to_github_app_install_page():
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/github-app/install")

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://github.com/apps/test-app-slug/installations/new"
    )


def test_callback_sets_signed_installation_cookie():
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            "/github-app/callback",
            params={"installation_id": 123, "setup_action": "install"},
        )

    assert response.status_code == 200
    assert response.json() == {"installation_id": "123", "setup_action": "install"}
    assert "installation_id" in response.cookies


def test_current_installation_without_cookie_returns_401():
    with TestClient(app) as client:
        response = client.get("/github-app/installations/current")

    assert response.status_code == 401


def test_current_installation_rejects_tampered_cookie():
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("installation_id", "not-a-validly-signed-value")
        response = client.get("/github-app/installations/current")

    assert response.status_code == 401


def test_current_installation_returns_account_and_repos(respx_mock):
    respx_mock.get("https://api.github.com/app/installations/123").mock(
        return_value=httpx.Response(
            200,
            json={"id": 123, "account": {"login": "harisharaju1"}},
        )
    )
    respx_mock.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_test_token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    )
    respx_mock.get("https://api.github.com/installation/repositories").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "repositories": [
                    {"id": 1, "full_name": "harisharaju1/codereview-agent", "private": False}
                ],
            },
        )
    )

    with TestClient(app, follow_redirects=False) as client:
        # Reuse the real /callback endpoint to obtain a correctly signed
        # cookie, rather than hand-constructing one — this also doubles as
        # an integration check that /callback's cookie is actually usable
        # by /installations/current afterward.
        client.get(
            "/github-app/callback",
            params={"installation_id": 123, "setup_action": "install"},
        )

        response = client.get("/github-app/installations/current")

    assert response.status_code == 200
    body = response.json()
    assert body["installation"]["account"]["login"] == "harisharaju1"
    assert body["repositories"]["repositories"][0]["full_name"] == ("harisharaju1/codereview-agent")
