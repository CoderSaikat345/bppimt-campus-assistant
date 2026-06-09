"""
dependencies/auth.py

Enterprise authentication dependency for the BPPIMT Campus Resource Assistant.

Security architecture
─────────────────────
This module implements the backend half of the zero-trust identity perimeter.
The frontend (NextAuth) performs OAuth, issues a Google ID token, and surfaces
it on the session.  Every FastAPI request then presents that token in an HTTP
Authorization header as:

    Authorization: Bearer <google_id_token>

This dependency:
  1. Extracts the Bearer token from the Authorization header.
  2. Verifies the token signature against Google's live public keys.
  3. Validates the token's audience claim against our OAuth client ID.
  4. Validates the token's expiry (handled automatically by the library).
  5. Asserts the email domain is exactly "bppimt.ac.in".
  6. Returns a typed, validated principal object to the route handler.

Threat model
────────────
• Expired tokens      → google.oauth2.id_token raises ValueError → 401
• Wrong audience      → raises ValueError → 401
• Forged signature    → raises ValueError → 401 (keys fetched from Google)
• Non-BPPIMT domain   → explicit 403 Forbidden (correct HTTP semantics:
                        authenticated but not authorised)
• Missing header      → 401 Unauthorized

Usage
─────
    from dependencies.auth import verify_enterprise_user, AuthenticatedUser

    @router.post("/v1/chat")
    async def chat(
        payload: ChatRequest,
        principal: AuthenticatedUser = Depends(verify_enterprise_user),
    ) -> ChatResponse:
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Annotated

import google.auth.transport.requests
import google.oauth2.id_token
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP Bearer scheme — auto-generates the WWW-Authenticate header on 401
# auto_error=False lets us emit our own structured error body instead of
# FastAPI's default plain-text 403.
# ---------------------------------------------------------------------------
_bearer_scheme = HTTPBearer(auto_error=False)

# Pre-compile the domain check pattern once at module load time.
# This is marginally faster than string operations inside the hot path.
_DOMAIN_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-z0-9._%+\-]+@" + re.escape(settings.allowed_email_domain) + r"$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Typed principal — returned to route handlers after successful verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """
    Immutable, validated representation of an authenticated BPPIMT user.

    All fields are extracted from the verified Google ID token payload.
    Downstream code may trust these values without further validation.
    """

    sub: str
    """Google's stable, unique user identifier (subject claim)."""

    email: str
    """Verified university email address (ends with @bppimt.ac.in)."""

    name: str
    """User's display name from their Google profile."""

    picture: str
    """URL of the user's Google profile picture."""

    domain: str
    """Extracted email domain — always 'bppimt.ac.in' for valid principals."""

    issued_at: int
    """Token issue timestamp (Unix epoch seconds)."""

    expires_at: int
    """Token expiry timestamp (Unix epoch seconds)."""


# ---------------------------------------------------------------------------
# Shared Google Auth transport — reused across requests to avoid redundant
# HTTP connections to Google's public key endpoint.
# ---------------------------------------------------------------------------
_google_request = google.auth.transport.requests.Request()


# ---------------------------------------------------------------------------
# Core dependency
# ---------------------------------------------------------------------------


async def verify_enterprise_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
) -> AuthenticatedUser:
    """
    FastAPI dependency that enforces the BPPIMT zero-trust identity perimeter.

    Raises:
        HTTPException 401: Token absent, malformed, expired, or has invalid signature.
        HTTPException 403: Token is cryptographically valid but the email domain
                           is not @bppimt.ac.in (authenticated, not authorised).

    Returns:
        AuthenticatedUser: Validated principal for the current request.
    """
    # ── Step 1: Extract token from Authorization header ───────────────────────
    if credentials is None or not credentials.credentials:
        logger.warning(
            "[Auth] Request rejected: Authorization header missing or empty."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header with Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token: str = credentials.credentials

    # ── Step 2 + 3 + 4: Cryptographic verification via Google's public keys ──
    # google.oauth2.id_token.verify_oauth2_token:
    #   • Fetches Google's public keys from accounts.google.com/o/oauth2/certs
    #     (cached internally with TTL from the Cache-Control header).
    #   • Verifies RS256 signature.
    #   • Validates `aud` == client_id.
    #   • Validates `iss` is accounts.google.com or accounts.google.com/r/..
    #   • Validates `exp` (raises if token is expired).
    try:
        id_info: dict[str, object] = google.oauth2.id_token.verify_oauth2_token(
            id_token=raw_token,
            request=_google_request,
            audience=settings.google_oauth_client_id,
            clock_skew_in_seconds=30,  # 30-second grace window for clock drift
        )
    except ValueError as exc:
        # ValueError is raised for ALL cryptographic failures:
        # bad signature, wrong audience, expired token, wrong issuer, etc.
        logger.warning(
            "[Auth] Token verification failed: %s",
            str(exc),
            # Do NOT log the raw token — it may contain sensitive claims.
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed. The token may be expired, malformed, or issued for a different audience.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # ── Step 5: Domain authorisation check ───────────────────────────────────
    # At this point the token's cryptographic integrity is confirmed.
    # We now assert that the authenticated user belongs to bppimt.ac.in.

    email: str = str(id_info.get("email", ""))
    email_verified: bool = bool(id_info.get("email_verified", False))

    # Google sets email_verified=true only after domain ownership is confirmed.
    # Reject unverified emails — they could represent pre-verification accounts.
    if not email_verified:
        logger.warning(
            "[Auth] FORBIDDEN — unverified email in token. email=%s", email
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email address has not been verified by Google. Access denied.",
        )

    # Regex match enforces: local-part@bppimt.ac.in — case-insensitive.
    if not _DOMAIN_PATTERN.match(email):
        logger.warning(
            "[Auth] FORBIDDEN — email domain not in allowlist. email=%s allowed_domain=%s",
            email,
            settings.allowed_email_domain,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access is restricted to @{settings.allowed_email_domain} accounts. "
                f"Presented email '{email}' is not authorised."
            ),
        )

    # ── Step 6: Build and return the validated principal ─────────────────────
    domain = email.split("@")[1].lower()

    principal = AuthenticatedUser(
        sub=str(id_info.get("sub", "")),
        email=email.lower(),
        name=str(id_info.get("name", "")),
        picture=str(id_info.get("picture", "")),
        domain=domain,
        issued_at=int(id_info.get("iat", 0)),
        expires_at=int(id_info.get("exp", 0)),
    )

    logger.info(
        "[Auth] ACCEPTED — verified enterprise user. sub=%s email=%s",
        principal.sub,
        principal.email,
    )

    return principal
