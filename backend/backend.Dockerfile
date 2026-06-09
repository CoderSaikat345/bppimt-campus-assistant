# ==========================================
# BUILDER STAGE
# ==========================================
FROM python:3.11-slim as builder

# Prevents Python from writing pyc files and buffers stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build essentials required for compiling C-extensions (like grpcio, uvloop)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy packaging files
COPY pyproject.toml ./

# Install pip and the project dependencies into /usr/local
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ==========================================
# RUNNER STAGE
# ==========================================
FROM python:3.11-slim as runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Create a non-root user and group
RUN groupadd -r bppimt && useradd -r -g bppimt bppimt

WORKDIR /app

# Copy the installed site-packages and binaries from the builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the actual application source code
COPY . .

# Change ownership of the app directory to the non-root user
RUN chown -R bppimt:bppimt /app

# Switch to the non-root user for security
USER bppimt

# Expose the port Cloud Run will route traffic to
EXPOSE $PORT

# Start the application using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
