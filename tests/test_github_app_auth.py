import jwt

from src.config.settings import get_settings
from src.services import github_app_auth


def test_build_app_jwt_has_expected_claims():
    settings = get_settings()

    token = github_app_auth.build_app_jwt(settings)

    # Decoding without verifying the signature here is intentional and
    # sufficient: this test is checking that *our* code put the right
    # claims in, not re-testing PyJWT's own signing correctness. Verifying
    # the signature would require handing this test the public half of the
    # test keypair, which conftest.py's fixture doesn't currently expose —
    # not worth the extra plumbing for what this test is actually asserting.
    payload = jwt.decode(token, options={"verify_signature": False})

    assert payload["iss"] == settings.github_app_id
    assert payload["exp"] > payload["iat"]
