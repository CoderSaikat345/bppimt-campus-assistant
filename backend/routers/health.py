"""
routers/health.py — Unauthenticated liveness probe endpoint.

Cloud Run requires a 200 OK from /health within the startup probe timeout.
This endpoint intentionally has NO authentication dependency so that the
infrastructure layer can check service health without a Bearer token.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Infrastructure"])


class HealthResponse(BaseModel):
    status: str
    service: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns 200 OK when the service is running. No authentication required.",
)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="bppimt-campus-resource-assistant",
    )
