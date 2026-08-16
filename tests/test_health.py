from fastapi.testclient import TestClient

from src.main import app


def test_health_returns_ok():
    # TestClient wraps the app so tests can send it fake HTTP requests
    # in-process — no real network socket, no server actually running.
    # Using it as `with TestClient(app) as client:` (rather than just
    # `client = TestClient(app)`) makes it run our app's lifespan function
    # (src/dependencies/http_client.py's `lifespan`) around the block, the
    # same way a real server run would — needed for any route that depends
    # on `app.state.http_client` existing.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
