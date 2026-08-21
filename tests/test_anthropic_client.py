import anthropic
from fastapi.testclient import TestClient

from src.dependencies.anthropic_client import get_anthropic_client
from src.main import app


# get_anthropic_client's real signature takes a FastAPI Request (so it can
# reach `request.app.state.anthropic_client`) — this is the minimal fake
# needed to call the function directly in a test without going through an
# actual HTTP request/route, the same shortcut FastAPI's own dependency
# injection takes advantage of every request (it just passes whatever
# object it built, real Request or otherwise, into the function).
class _FakeRequest:
    def __init__(self, fastapi_app):
        self.app = fastapi_app


def test_anthropic_client_is_created_on_startup_and_shared():
    # `with TestClient(app):` is what actually triggers main.py's composed
    # lifespan (entering both http_client's and anthropic_client's context
    # managers) — outside this block, app.state.anthropic_client wouldn't
    # exist at all yet.
    with TestClient(app):
        request_stub = _FakeRequest(app)
        first = get_anthropic_client(request_stub)
        second = get_anthropic_client(request_stub)

    # Same object both times — proves this is one shared client, not a
    # fresh one constructed per call, the entire point of routing every
    # caller through this dependency instead of constructing their own.
    assert first is second
    assert isinstance(first, anthropic.AsyncAnthropic)
