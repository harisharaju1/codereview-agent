import httpx
from fastapi.testclient import TestClient

from src.main import app


def test_login_redirects_to_github_with_state_and_sets_cookie():
    # `follow_redirects=False` tells TestClient to hand back the raw 307
    # response instead of automatically chasing the redirect to
    # github.com — which we don't want, since these tests never actually
    # talk to the real GitHub.
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/github/login")

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")
    assert "client_id=test-client-id" in location
    assert "scope=public_repo" in location
    assert "state=" in location
    assert "oauth_state" in response.cookies


def test_callback_rejects_mismatched_state():
    with TestClient(app, follow_redirects=False) as client:
        # `client.cookies.set(...)` manually plants a cookie on the test
        # client, simulating "the browser already has this cookie from an
        # earlier /login response" without actually calling /login first.
        client.cookies.set("oauth_state", "expected-state")
        response = client.get(
            "/auth/github/callback",
            params={"code": "irrelevant", "state": "wrong-state"},
        )

    assert response.status_code == 400
    assert "session" not in response.cookies


def test_callback_rejects_missing_state_cookie():
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(
            "/auth/github/callback",
            params={"code": "irrelevant", "state": "some-state"},
        )

    assert response.status_code == 400


def test_callback_success_sets_session_and_returns_user(respx_mock):
    # `respx_mock` is a fixture respx registers automatically once it's
    # installed — for the duration of this test, it intercepts any httpx
    # request matching the URLs below and returns the canned response
    # instead of making a real network call. This is how we test the OAuth
    # exchange without ever hitting GitHub's real servers.
    respx_mock.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "gho_test_token",
                "token_type": "bearer",
                "scope": "public_repo",
            },
        )
    )
    respx_mock.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "login": "octocat",
                "id": 1,
                "name": "The Octocat",
                "avatar_url": "https://example.com/avatar.png",
            },
        )
    )

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("oauth_state", "matching-state")
        response = client.get(
            "/auth/github/callback",
            params={"code": "real-code", "state": "matching-state"},
        )

        assert response.status_code == 200
        assert response.json()["login"] == "octocat"
        assert "session" in response.cookies

        # Reusing the same `client` (still inside the `with` block) means
        # the session cookie /callback just set is carried over
        # automatically to this next request — proving the login "sticks"
        # across requests, the way a real browser session would.
        me_response = client.get("/auth/me")

    assert me_response.status_code == 200
    assert me_response.json()["login"] == "octocat"


def test_me_without_session_returns_401():
    with TestClient(app) as client:
        response = client.get("/auth/me")

    assert response.status_code == 401


def test_me_rejects_tampered_session_cookie():
    with TestClient(app, follow_redirects=False) as client:
        # A cookie value that was never produced by sign_session_id(...) —
        # this proves the itsdangerous signature check in
        # get_current_user actually rejects forged/garbage input, not just
        # a missing cookie.
        client.cookies.set("session", "this-is-not-a-validly-signed-value")
        response = client.get("/auth/me")

    assert response.status_code == 401
