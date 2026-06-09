"""
tests/test_auth.py

Unit + integration tests for the verify_enterprise_user dependency.

Test strategy:
  • Mock google.oauth2.id_token.verify_oauth2_token to avoid network calls.
  • Test each rejection path independently.
  • Verify the AuthenticatedUser dataclass is populated correctly on success.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

# Module under test
from dependencies.auth import AuthenticatedUser, verify_enterprise_user

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

VALID_TOKEN_PAYLOAD: dict[str, object] = {
    "sub": "115329485732948576123",
    "email": "john.doe@bppimt.ac.in",
    "email_verified": True,
    "name": "John Doe",
    "picture": "https://lh3.googleusercontent.com/a/example",
    "iat": 1700000000,
    "exp": 1700028800,
    "aud": "fake-client-id.apps.googleusercontent.com",
    "iss": "accounts.google.com",
}


def _make_credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMissingToken:
    @pytest.mark.asyncio
    async def test_returns_401_when_no_authorization_header(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await verify_enterprise_user(credentials=None)

        assert exc_info.value.status_code == 401
        assert "WWW-Authenticate" in exc_info.value.headers


class TestCryptographicVerification:
    @pytest.mark.asyncio
    async def test_returns_401_on_invalid_signature(self) -> None:
        credentials = _make_credentials("forged.token.value")

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            side_effect=ValueError("Token signature is invalid."),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_401_on_expired_token(self) -> None:
        credentials = _make_credentials("expired.token.value")

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            side_effect=ValueError("Token expired."),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_401_on_wrong_audience(self) -> None:
        credentials = _make_credentials("wrong.audience.token")

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            side_effect=ValueError("Token has wrong audience."),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 401


class TestDomainEnforcement:
    @pytest.mark.asyncio
    async def test_returns_403_for_gmail_account(self) -> None:
        credentials = _make_credentials("valid.but.wrong.domain")
        gmail_payload = {**VALID_TOKEN_PAYLOAD, "email": "attacker@gmail.com"}

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            return_value=gmail_payload,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 403
            assert "bppimt.ac.in" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_returns_403_for_similar_domain(self) -> None:
        """Ensure 'fakebppimt.ac.in' is not confused with 'bppimt.ac.in'."""
        credentials = _make_credentials("valid.similar.domain")
        spoof_payload = {**VALID_TOKEN_PAYLOAD, "email": "user@fakebppimt.ac.in"}

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            return_value=spoof_payload,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_403_for_unverified_email(self) -> None:
        credentials = _make_credentials("unverified.email.token")
        unverified_payload = {**VALID_TOKEN_PAYLOAD, "email_verified": False}

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            return_value=unverified_payload,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_enterprise_user(credentials=credentials)

            assert exc_info.value.status_code == 403


class TestSuccessfulVerification:
    @pytest.mark.asyncio
    async def test_returns_authenticated_user_for_valid_bppimt_token(self) -> None:
        credentials = _make_credentials("valid.bppimt.token")

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            return_value=VALID_TOKEN_PAYLOAD,
        ):
            principal = await verify_enterprise_user(credentials=credentials)

        assert isinstance(principal, AuthenticatedUser)
        assert principal.email == "john.doe@bppimt.ac.in"
        assert principal.domain == "bppimt.ac.in"
        assert principal.sub == VALID_TOKEN_PAYLOAD["sub"]
        assert principal.name == "John Doe"

    @pytest.mark.asyncio
    async def test_email_is_lowercased_in_principal(self) -> None:
        """Ensure mixed-case emails in token are normalised to lowercase."""
        credentials = _make_credentials("mixed.case.token")
        mixed_case_payload = {
            **VALID_TOKEN_PAYLOAD,
            "email": "John.DOE@BPPIMT.AC.IN",
        }

        with patch(
            "dependencies.auth.google.oauth2.id_token.verify_oauth2_token",
            return_value=mixed_case_payload,
        ):
            principal = await verify_enterprise_user(credentials=credentials)

        assert principal.email == "john.doe@bppimt.ac.in"
        assert principal.domain == "bppimt.ac.in"
