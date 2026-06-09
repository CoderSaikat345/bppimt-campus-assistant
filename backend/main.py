"""
main.py — FastAPI application factory for the BPPIMT Campus Resource Assistant.

Architecture decisions:
  • Lifespan context manager (not deprecated on_event hooks) for startup/shutdown.
  • CORS restricted to known frontend origins from settings.
  • /health is publicly accessible (required by Cloud Run liveness probes).
  • All other routes are mounted under /v1 and require a valid Bearer token.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from config import settings
from routers import chat, health

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan handler.

    Startup:  Initialise heavy singletons (LlamaIndex engine, Vertex AI client)
              so they are ready before the first request.
    Shutdown: Gracefully release connections and flush buffers.
    """
    logger.info(
        "Starting BPPIMT Campus Resource Assistant backend. "
        "environment=%s project=%s",
        settings.environment,
        settings.google_cloud_project,
    )
    # Lazy-import to avoid circular dependencies and allow mocking in tests.
    from services.rag_engine import RAGEngine  # noqa: PLC0415

    app.state.rag_engine = await RAGEngine.create()
    logger.info("RAG engine initialised successfully.")

    yield  # Application runs here

    logger.info("Shutting down BPPIMT Campus Resource Assistant backend.")
    try:
        await app.state.rag_engine.close()
    except Exception as exc:
        # Log but do not re-raise — shutdown must always complete gracefully
        logger.warning("Error during RAG engine shutdown: %s", exc)


def create_application() -> FastAPI:
    app = FastAPI(
        title="BPPIMT Campus Resource Assistant API",
        description=(
            "Enterprise RAG-based chatbot API restricted to @bppimt.ac.in domain users. "
            "All endpoints (except /health) require a valid Google ID token."
        ),
        version="1.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware stack (applied in reverse order) ───────────────────────────

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.backend_cors_origins],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    # ── Routers ───────────────────────────────────────────────────────────────

    app.include_router(health.router)               # GET /health (public)
    app.include_router(chat.router, prefix="/api/v1")  # /api/v1/chat + /api/v1/chat/stream

    return app


app = create_application()
