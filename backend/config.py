"""
config.py — Single source of truth for all backend configuration.

All settings are read from environment variables.  Pydantic's BaseSettings
performs type coercion, validation, and raises a clear ValidationError at
startup if any required variable is absent — preventing the service from
starting in a misconfigured state.

Usage:
    from config import settings
    settings.google_oauth_client_id
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application-wide settings sourced exclusively from environment variables.

    In local development: place values in backend/.env
    In Cloud Run: inject via Secret Manager environment variable references.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Forbid extra keys — surfaces typos in .env files immediately.
        extra="forbid",
    )

    # ── Identity & Security ───────────────────────────────────────────────────

    google_oauth_client_id: Annotated[
        str,
        Field(
            ...,
            description=(
                "Google OAuth 2.0 Client ID. Used by google.oauth2.id_token "
                "to verify the audience claim in incoming Bearer tokens."
            ),
        ),
    ]

    allowed_email_domain: Annotated[
        str,
        Field(
            default="bppimt.ac.in",
            description="Only tokens whose email ends with this domain are accepted.",
        ),
    ]

    # ── CORS ─────────────────────────────────────────────────────────────────

    backend_cors_origins: Annotated[
        list[AnyHttpUrl],
        Field(
            ...,
            description=(
                "Comma-separated list of allowed CORS origins. "
                "Example: http://localhost:3000,https://bppimt-assistant.vercel.app"
            ),
        ),
    ]

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, raw: str | list[str]) -> list[str]:
        """Accept either a JSON list or a comma-separated string from env."""
        if isinstance(raw, str):
            return [origin.strip() for origin in raw.split(",") if origin.strip()]
        return raw

    # ── Google Cloud ─────────────────────────────────────────────────────────

    google_cloud_project: Annotated[
        str,
        Field(..., description="GCP project ID for Vertex AI and BigQuery."),
    ]

    vertex_ai_location: Annotated[
        str,
        Field(default="us-central1", description="Vertex AI region."),
    ]

    # ── BigQuery Vector Search ────────────────────────────────────────────────

    bigquery_dataset: Annotated[
        str,
        Field(..., description="BigQuery dataset containing the vector index table."),
    ]

    bigquery_table: Annotated[
        str,
        Field(..., description="BigQuery table used as the vector search index."),
    ]

    # ── Application ───────────────────────────────────────────────────────────

    environment: Annotated[
        str,
        Field(
            default="development",
            description="Runtime environment: development | staging | production.",
        ),
    ]

    google_application_credentials: Annotated[
        str | None,
        Field(
            default=None,
            description="Path to GCP service account JSON key file, automatically used by Google Auth.",
        ),
    ]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    log_level: Annotated[
        str,
        Field(default="INFO", description="Uvicorn/application log level."),
    ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return a cached singleton Settings instance.

    The lru_cache ensures environment variables are read exactly once at
    startup and that all modules share the same validated configuration object.
    """
    return Settings()  # type: ignore[call-arg]


# Module-level convenience alias — import this in other modules.
settings: Settings = get_settings()
