import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.schemas.review import ReviewResult, ReviewUsage
from src.services import installation_token_cache
from src.services.review_engines import ENGINES


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

    # The router looks engines up in review_engines.ENGINES at request
    # time, so replacing the dict ENTRY is what intercepts the call (the
    # dict is the same object the router imported — "patch where it's
    # looked up" applied to a registry instead of a module-level name).
    monkeypatch.setitem(ENGINES, "loop", _raise_connection_error)

    with _authenticated_client() as client:
        response = client.post("/github-app/repos/owner/repo/pulls/1/review")

    assert response.status_code == 502
    assert response.json()["detail"] == "Claude API request failed"


def test_review_pull_request_includes_usage_in_agent_failure_response(respx_mock, monkeypatch):
    respx_mock.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )

    from src.schemas.review import ReviewUsage
    from src.services.review_tools import AgentExceededMaxIterationsError

    async def _raise_exceeded(*args, **kwargs):
        raise AgentExceededMaxIterationsError(
            "Agent exceeded 8 iterations without calling submit_review",
            usage=ReviewUsage(input_tokens=800, output_tokens=160, estimated_cost_usd=0.0016),
        )

    monkeypatch.setitem(ENGINES, "loop", _raise_exceeded)

    with _authenticated_client() as client:
        response = client.post("/github-app/repos/owner/repo/pulls/1/review")

    assert response.status_code == 502
    body = response.json()["detail"]
    assert "exceeded 8 iterations" in body["error"]
    assert body["usage"] == {
        "input_tokens": 800,
        "output_tokens": 160,
        "estimated_cost_usd": 0.0016,
    }


def _mock_installation_token(respx_mock):
    respx_mock.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_test_token", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )


def test_review_pull_request_dispatches_to_the_requested_engine(respx_mock, monkeypatch):
    _mock_installation_token(respx_mock)
    called_engines: list[str] = []

    def _fake_engine(name: str):
        async def _run(*args, **kwargs):
            called_engines.append(name)
            return ReviewResult(
                findings=[],
                usage=ReviewUsage(input_tokens=1, output_tokens=1, estimated_cost_usd=None),
            )

        return _run

    for name in ENGINES:
        monkeypatch.setitem(ENGINES, name, _fake_engine(name))

    with _authenticated_client() as client:
        default_response = client.post("/github-app/repos/owner/repo/pulls/1/review")
        graph_response = client.post("/github-app/repos/owner/repo/pulls/1/review?engine=graph")

    assert default_response.status_code == 200
    assert graph_response.status_code == 200
    # No engine param means the Week 2 loop — the default stays "loop"
    # until Day 3's measurements justify changing it.
    assert called_engines == ["loop", "graph"]


def test_review_pull_request_rejects_unknown_engine_with_422(respx_mock):
    _mock_installation_token(respx_mock)

    with _authenticated_client() as client:
        response = client.post("/github-app/repos/owner/repo/pulls/1/review?engine=bogus")

    assert response.status_code == 422
