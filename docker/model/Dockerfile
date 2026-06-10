# =============================================================================
# docker/model/Dockerfile
#
# OpenAgent Inference Proxy Container
#
# Design decisions
# ---------------------------------------
# 1. python:3.12-slim base image — with the base model, the nervous-system
#    model, and the embedding model all running on external BYOC provider
#    endpoints, this container only runs the FastAPI proxy layer
#    (src/api/main.py). There is no local GPU, no model loading, no CUDA stack
#    needed here. The image stays under 500 MB.
#
# 2. No CUDA, no PyTorch, no GPU-related dependencies — all inference happens
#    at the provider. This container is a pure Python web application:
#    FastAPI + uvicorn + httpx.
#
# 3. No volume mount — no model weights live here. Nothing to pull, nothing
#    to mount.
#
# 4. No NVIDIA toolkit, no deploy.resources.reservations block needed in
#    docker-compose.yml — this container has no GPU dependency.
#
# 5. All secrets (API_KEY, PROVIDER_API_KEY) and tuneable values
#    (BASE_MODEL_URL, NERVOUS_SYSTEM_URL, EMBEDDING_MODEL_URL, REASONING_EFFORT,
#    and the optional BASE_MODEL_NAME / NERVOUS_SYSTEM_MODEL_NAME /
#    EMBEDDING_MODEL_NAME) are passed via the root .env file — never hardcoded
#    here.
#
# 6. This Dockerfile serves one service in docker-compose.yml:
#      openagent-infra — runs the FastAPI proxy on port 8002.
#    The proxy validates X-API-Key auth on every request. On /chat it injects
#    reasoning_effort into the system message, routes to the correct provider
#    endpoint based on the optional model field, and streams the SSE response
#    back. On /embed it forwards the input to the embedding model and returns
#    the provider's JSON response (no streaming).
#
# 7. Port topology:
#      openagent-api (:8001) → openagent-infra (:8002) → BYOC base model           [/chat, default]
#                                                       → BYOC nervous-system model [/chat, model="nervous_system"]
#                                                       → BYOC embedding model      [/embed]
#    openagent-infra is the only service in this repo. All models run on
#    external provider endpoints — no inference happens here. api is the
#    primary caller today; other server-side services may call the proxy as
#    the system grows.
#
# Build (from project root)
# -------------------------
#   docker build -f docker/model/Dockerfile -t openagent-infra:latest .
#
# Run via docker-compose (recommended — see docker-compose.yml at root)
#   docker-compose up openagent-infra
# =============================================================================


# -----------------------------------------------------------------------------
# Base image — python:3.12-slim
#
# Minimal Debian-based Python 3.12 image. No CUDA, no PyTorch, no GPU tooling —
# none of that is needed here. The proxy is a pure async Python web app.
# Pinned to 3.12-slim for reproducibility.
# -----------------------------------------------------------------------------
FROM python:3.12-slim


# -----------------------------------------------------------------------------
# Labels — human-readable metadata, visible via `docker inspect`
# -----------------------------------------------------------------------------
LABEL maintainer="William McKeon"
LABEL description="OpenAgent inference proxy — routes requests to the base, nervous-system, and embedding models on a BYOC provider"


# -----------------------------------------------------------------------------
# Environment — system level
#
# PYTHONUNBUFFERED=1
#   Disables Python output buffering so every log line appears in
#   docker-compose logs immediately. Critical for streaming proxy visibility.
#
# PYTHONDONTWRITEBYTECODE=1
#   Stops Python writing .pyc files inside the container.
#
# PIP_NO_CACHE_DIR=1
#   Prevents pip storing its download cache inside the image layer.
#
# DEBIAN_FRONTEND=noninteractive
#   Suppresses apt interactive prompts during build.
# -----------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive


# -----------------------------------------------------------------------------
# Environment — application level defaults
#
# These match the os.environ.get() calls in src/api/main.py exactly.
# All are overridable via the root .env file without rebuilding the image.
# Empty-string defaults here — all required values must be set in .env.
#
# API_KEY
#   Secret validated against the X-API-Key header on every /chat and /embed
#   request. The same value openagent-api holds as INFRA_API_KEY. Required.
#
# BASE_MODEL_URL
#   OpenAI-compatible BASE endpoint for the base model — the provider root,
#   WITHOUT the chat-completions path. The proxy appends /v1/chat/completions
#   when forwarding. Default route for all /chat requests. Required.
#   e.g. https://your-provider.com/openai
#
# NERVOUS_SYSTEM_URL
#   OpenAI-compatible BASE endpoint for the nervous-system (control-layer)
#   model — same base form as BASE_MODEL_URL (no /v1/chat/completions; the
#   proxy appends it). Used when model="nervous_system" is passed.
#   Optional — without it, model="nervous_system" requests fail gracefully and
#   the pipeline is unbroken (omitting the model field always routes to base).
#
# EMBEDDING_MODEL_URL
#   OpenAI-compatible BASE endpoint for the embedding model — same base form
#   as BASE_MODEL_URL (no path; the proxy appends /v1/embeddings). Used by
#   POST /embed. Optional — without it, /embed returns a clear "not configured"
#   error and /chat is entirely unaffected.
#
# PROVIDER_API_KEY
#   Sent as Authorization: Bearer on every forwarded request to the provider
#   endpoint(s). Separate from API_KEY, which faces the caller — this key
#   faces the provider. Never exposed to any caller. Required.
#
# REASONING_EFFORT
#   Default reasoning effort applied to both chat models if not set per request
#   (the embedding model does not reason).
#   low | medium (default) | high. Overridable per request.
#
# BASE_MODEL_NAME / NERVOUS_SYSTEM_MODEL_NAME / EMBEDDING_MODEL_NAME
#   Optional model name per route. When set, the proxy adds "model": <name> to
#   that route's forwarded payload; when empty, no model field is sent (the
#   default). Required by providers that demand an explicit model field — e.g.
#   a BGE-M3 embedding worker returns 500 without it (set EMBEDDING_MODEL_NAME=
#   BAAI/bge-m3). Routing is by URL regardless; these only shape the payload.
#   All empty by default; real values come from .env.
# -----------------------------------------------------------------------------
ENV API_KEY="" \
    BASE_MODEL_URL="" \
    NERVOUS_SYSTEM_URL="" \
    EMBEDDING_MODEL_URL="" \
    PROVIDER_API_KEY="" \
    REASONING_EFFORT="medium" \
    BASE_MODEL_NAME="" \
    NERVOUS_SYSTEM_MODEL_NAME="" \
    EMBEDDING_MODEL_NAME=""


# -----------------------------------------------------------------------------
# Working directory
# -----------------------------------------------------------------------------
WORKDIR /app


# -----------------------------------------------------------------------------
# Python dependencies
#
# requirements.txt is copied before src/ so Docker can cache this layer.
# A code-only change will not re-run pip install.
#
# Packages installed:
#   fastapi        — web framework for the proxy API
#   uvicorn        — ASGI server that runs the FastAPI app
#   httpx          — async HTTP client for forwarding to the provider
#   python-dotenv  — loads .env for local (non-Docker) development
#
# No torch, transformers, accelerate, or any GPU/ML libraries — all inference
# runs at the provider.
# -----------------------------------------------------------------------------
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# -----------------------------------------------------------------------------
# Application source code
#
# Only src/ is needed at runtime. Project files (README, .env.example,
# .gitignore, etc.) are excluded via .dockerignore at project root.
# -----------------------------------------------------------------------------
COPY src/ ./src/


# -----------------------------------------------------------------------------
# Default command
#
# Runs the FastAPI proxy via uvicorn on port 8002. The only service this
# image runs. The proxy:
#   - Validates X-API-Key on every /chat and /embed request
#   - /chat  : injects Reasoning: <level> into the system message, routes to
#              BASE_MODEL_URL (default) or NERVOUS_SYSTEM_URL based on the
#              optional model field, and streams the SSE response back
#   - /embed : forwards the input to EMBEDDING_MODEL_URL and returns the
#              provider's JSON embeddings response (no streaming)
#   - Probes all three provider endpoints (base, nervous-system, embedding)
#     on GET /health
# -----------------------------------------------------------------------------
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8002"]