# syntax=docker/dockerfile:1.7

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools needed for packages with C extensions (e.g. grpcio).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

# Install dependencies into a prefix directory so Stage 2 can copy just that.
RUN pip install --no-cache-dir --prefix=/install ".[standard]" 2>/dev/null || \
    pip install --no-cache-dir --prefix=/install \
        fastapi[standard] \
        uvicorn[standard] \
        pydantic \
        pydantic-settings \
        google-auth \
        google-cloud-aiplatform \
        google-cloud-bigquery \
        llama-index \
        llama-index-llms-vertex \
        llama-index-embeddings-vertex \
        python-multipart \
        httpx

# ── Stage 2: minimal runtime image ───────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for zero-privilege runtime — Cloud Run best practice.
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy pre-built dependencies from builder stage.
COPY --from=builder /install /usr/local

# Copy application source.
COPY --chown=appuser:appgroup . .

USER appuser

# Cloud Run injects PORT env var; default to 8080 for local docker run.
ENV PORT=8080

EXPOSE 8080

# Use exec form to receive signals correctly (enables graceful shutdown).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info"]
