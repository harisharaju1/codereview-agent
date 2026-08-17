import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config.settings import get_settings

# pytest automatically discovers a file named conftest.py and treats
# anything defined in it (fixtures, in particular) as available to every
# test in this directory and below — no import needed in the test files
# themselves.


# `scope="session"` means this fixture runs once for the whole test run,
# not once per test — generating a 2048-bit RSA key isn't free, and every
# test needing a private key file can safely share the same one, since
# nothing about the key's actual content matters for what these tests
# assert (they check that our own JWT-building code works, not that a
# specific key's bytes appear anywhere).
@pytest.fixture(scope="session")
def _test_github_app_private_key_path(tmp_path_factory):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path_factory.mktemp("keys") / "test-github-app-private-key.pem"
    key_path.write_bytes(pem_bytes)
    return str(key_path)


# `@pytest.fixture(autouse=True)` means this fixture runs for *every* test
# in this directory automatically, without any test needing to explicitly
# request it as an argument.
@pytest.fixture(autouse=True)
def _default_env(_test_github_app_private_key_path, monkeypatch):
    # `monkeypatch` is a built-in pytest fixture for temporarily patching
    # things (env vars, attributes, dict entries) and having them
    # automatically reverted after the test ends — regardless of whether
    # the test passed, failed, or raised. Using it instead of directly
    # mutating os.environ means one test's env changes can never leak into
    # the next test.
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret-key")
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", _test_github_app_private_key_path)
    monkeypatch.setenv("GITHUB_APP_SLUG", "test-app-slug")

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
