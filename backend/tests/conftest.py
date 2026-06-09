"""
tests/conftest.py

Shared pytest fixtures for all backend tests.
"""

from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True)
def _patch_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Inject the minimum required environment variables so that config.py
    (which calls get_settings() at module-import time) does not raise a
    ValidationError during test collection.

    These values are safe test-only placeholders — they never reach GCP.
    """
    env_overrides = {
        "GOOGLE_OAUTH_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "ALLOWED_EMAIL_DOMAIN": "bppimt.ac.in",
        "BACKEND_CORS_ORIGINS": "http://localhost:3000",
        "GOOGLE_CLOUD_PROJECT": "test-gcp-project",
        "VERTEX_AI_LOCATION": "us-central1",
        "BIGQUERY_DATASET": "test_dataset",
        "BIGQUERY_TABLE": "test_vector_table",
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "WARNING",
    }
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
