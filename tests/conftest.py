import pytest

from src.config.settings import get_settings

# pytest automatically discovers a file named conftest.py and treats
# anything defined in it (fixtures, in particular) as available to every
# test in this directory and below — no import needed in the test files
# themselves.


# `@pytest.fixture(autouse=True)` means this fixture runs for *every* test
# in this directory automatically, without any test needing to explicitly
# request it as an argument.
@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    # `monkeypatch` is a built-in pytest fixture for temporarily patching
    # things (env vars, attributes, dict entries) and having them
    # automatically reverted after the test ends — regardless of whether
    # the test passed, failed, or raised. Using it instead of directly
    # mutating os.environ means one test's env changes can never leak into
    # the next test.
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret-key")

    # get_settings() is lru_cache'd for the running app, but each test may
    # set its own env vars — clear the cache so every test sees a fresh
    # Settings() built from its own env, not a stale one from an earlier test.
    get_settings.cache_clear()
    # A fixture that `yield`s (instead of just `return`ing) splits into a
    # "setup" part (everything before yield) and a "teardown" part
    # (everything after) — pytest runs setup, then the test itself, then
    # comes back to run teardown once the test is done. This is the same
    # setup/teardown shape as the `@asynccontextmanager` pattern used in
    # src/dependencies/http_client.py, just via a different pytest-specific
    # mechanism.
    yield
    get_settings.cache_clear()
