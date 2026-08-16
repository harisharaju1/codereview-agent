import pytest
from pydantic import ValidationError

from src.config.settings import Settings

# pytest discovers tests by naming convention: any function named
# `test_*` in a file named `test_*.py` is picked up and run automatically —
# no test registry or decorator needed, unlike some other frameworks.


def test_settings_defaults_to_development(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    # `_env_file=None` overrides the class-level `env_file=".env"` config
    # just for this one instance, so this test isn't accidentally affected
    # by a real .env file sitting in the project root (e.g. the one you
    # create locally with real GitHub credentials).
    settings = Settings(_env_file=None)

    assert settings.environment == "development"


def test_settings_accepts_valid_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")

    settings = Settings()

    assert settings.environment == "production"


def test_settings_rejects_invalid_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "not-a-real-environment")

    # `pytest.raises(...)` as a `with` block asserts that the code inside it
    # raises the given exception type — the test fails if the block
    # completes without raising, or raises something else.
    with pytest.raises(ValidationError):
        Settings()
