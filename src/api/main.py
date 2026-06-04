"""
src/api/main.py

OpenAgent inference API — thin FastAPI proxy layer.

This file is the public-facing API for openagent-infra. It sits between
openagent-api (its only caller) and one or two external compute providers,
handling authentication and request validation before forwarding. It streams
the provider's response directly back to the caller as Server-Sent Events.

Architecture
---------------------------------------
                openagent-api
                      │
                      │  POST /chat  (X-API-Key, messages, reasoning_effort, model?)
                      ▼
              src/api/main.py          ← YOU ARE HERE
              FastAPI proxy (port 8002)
              - Validates X-API-Key
              - Validates request body
              - Injects reasoning_effort into the system message
              - Routes by the optional `model` field
              - Forwards to the BYOC provider
              - Streams the SSE response back byte-for-byte
                      │
                      │  POST <BASE_MODEL_URL | NERVOUS_SYSTEM_URL>
                      ▼
              BYOC Compute Provider    (any OpenAI-compatible endpoint)
              - model="base" (default)  → BASE_MODEL_URL      (base reasoning model)
              - model="nervous_system"  → NERVOUS_SYSTEM_URL  (fast control model)
              - OpenAI-compatible /chat/completions, SSE streaming

Why a proxy layer instead of exposing the provider directly
---------------------------------------
A provider usually offers a single shared API key. Keeping FastAPI as a proxy
puts caller authentication in one place and keeps the provider's billing
credential out of the gateway and frontend layers. It also gives a stable,
OpenAI-shaped contract regardless of which provider sits behind it. The
validation logic is isolated in verify_api_key so it can later be swapped
from a static .env key to a per-user lookup without touching the endpoint
contract or the provider configuration.

System prompt ownership
---------------------------------------
The persona is owned by openagent-api, not openagent-infra. openagent-api
sends it as the first system message in the messages list on every request.
This file does not add, modify, or inspect any messages — it forwards the
full list to the provider exactly as received, with the reasoning_effort
instruction injected into the existing system message automatically.

Reasoning effort
---------------------------------------
Both the base model and the nervous-system model support configurable
reasoning effort — low, medium, high. The effort level is injected into the
system message before forwarding. Default is medium. openagent-api or any
tooling can set it per request based on the complexity of the task:
  - low    : fast responses, lightweight tooling calls, simple queries
  - medium : standard interactions (default)
  - high   : complex reasoning, deep analysis, multi-step tasks

Endpoints
---------------------------------------
POST /chat
    Header       : X-API-Key: <your_api_key>
    Request body : {
                     "messages": [...],            # OpenAI messages format
                     "reasoning_effort": "medium", # optional, default: medium
                     "model": "base"               # optional, default: base
                                                   # "base"           → base model
                                                   # "nervous_system" → control model
                   }
    Response     : text/event-stream (SSE)
                   Each event payload is a JSON ChatCompletion chunk,
                   terminating with: data: [DONE]\n\n

GET /health
    Returns {"status": "ok" | "degraded", ...} — no auth required.
    Checks the proxy and both provider endpoints.

Usage
---------------------------------------
Start via docker-compose:
    docker-compose up openagent-infra

Or directly with uvicorn for local dev:
    uvicorn src.api.main:app --host 0.0.0.0 --port 8002 --reload

Test with curl:
    curl -X POST http://localhost:8002/chat \\
         -H "Content-Type: application/json" \\
         -H "X-API-Key: your_api_key_here" \\
         -d '{"messages": [{"role": "system", "content": "You are OpenAgent..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \\
         --no-buffer

    curl http://localhost:8002/health
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# Load .env for local (non-Docker) development. Under docker-compose the
# values arrive via env_file, so this is a no-op there.
load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("openagent-infra")


# ---------------------------------------------------------------------------
# Environment configuration
#
# API_KEY             : Secret validated against the X-API-Key header on /chat.
#                       The same value openagent-api holds as INFRA_API_KEY.
#                       Isolated in verify_api_key for a future per-user swap.
# BASE_MODEL_URL      : OpenAI-compatible chat-completions endpoint for the
#                       base reasoning model. Default route for all /chat
#                       requests (model="base" or no model field). Full URL,
#                       e.g. https://your-provider.com/v1/chat/completions
# NERVOUS_SYSTEM_URL  : OpenAI-compatible chat-completions endpoint for the
#                       fast control model. Used when model="nervous_system".
#                       Optional — when unset, that route is "not configured".
# PROVIDER_API_KEY    : Sent as Authorization: Bearer on every forwarded
#                       request to the provider endpoint(s). Never exposed
#                       to any caller. Required.
# REASONING_EFFORT    : Default reasoning effort if not set per request.
#                       Applies to both models. low | medium (default) | high.
#
# No model name is sent in the payload — each provider endpoint serves a fixed
# model. The proxy routes by URL selection, not by a model name in the body.
# ---------------------------------------------------------------------------
API_KEY            = os.environ.get("API_KEY", "")
BASE_MODEL_URL     = os.environ.get("BASE_MODEL_URL", "")
NERVOUS_SYSTEM_URL = os.environ.get("NERVOUS_SYSTEM_URL", "")
PROVIDER_API_KEY   = os.environ.get("PROVIDER_API_KEY", "")
REASONING_EFFORT   = os.environ.get("REASONING_EFFORT", "medium")


# ---------------------------------------------------------------------------
# API key authentication
#
# Every /chat request must include a valid X-API-Key header.
# Requests with missing or invalid keys receive 401 Unauthorized.
#
# Design note: the validation logic is isolated in verify_api_key so it can
# be swapped from a static .env check to a per-user lookup without touching
# the endpoint logic.
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Security(api_key_header)) -> str:
    """
    FastAPI dependency that validates the X-API-Key header.

    Raises 401 if the header is missing or the key does not match API_KEY.
    Returns the key on success.
    """
    if not key or key != API_KEY:
        logger.warning("Unauthorized /chat request — invalid or missing API key")
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
        )
    return key


# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown logging
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    The proxy has no model to load — the provider owns that.
    Lifespan is used for logging only.
    """
    logger.info("=== OpenAgent Inference API Starting ===")
    logger.info("Proxy port            : 8002")
    logger.info("Base model endpoint   : %s", BASE_MODEL_URL or "NOT SET")
    logger.info("Nervous system URL    : %s", NERVOUS_SYSTEM_URL or "NOT SET")
    logger.info("Default reasoning     : %s", REASONING_EFFORT)
    logger.info("Provider API key      : ...%s", PROVIDER_API_KEY[-4:] if PROVIDER_API_KEY else "NOT SET")
    logger.info("=== OpenAgent Inference API Ready — listening on :8002 ===")

    yield

    logger.info("=== OpenAgent Inference API Shutting Down ===")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="OpenAgent Inference API",
    description="Model inference proxy — the model serving layer of the OpenAgent system.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------
class Message(BaseModel):
    """Single message in the OpenAI messages format."""
    role: str
    content: str


class ChatRequest(BaseModel):
    """
    Request body for POST /chat.

    messages         : Full OpenAI messages list. The caller (openagent-api)
                       is responsible for including the persona as the first
                       system message. This proxy does not add or modify
                       messages except to inject the reasoning_effort level
                       into the existing system message automatically.

    reasoning_effort : Controls how much reasoning the model applies before
                       answering. Optional — defaults to the REASONING_EFFORT
                       env var (medium).
                         low    — fast, lightweight tooling calls
                         medium — standard interactions (default)
                         high   — complex reasoning, deep analysis

    model            : Selects which endpoint handles the request.
                       Optional — defaults to "base".
                         "base"           — base reasoning model
                         "nervous_system" — fast control model
                       Omitting this field always routes to the base model.

    Example — standard conversation (routes to the base model):
        {
          "messages": [
            {"role": "system", "content": "You are OpenAgent..."},
            {"role": "user",   "content": "explain quantum entanglement"}
          ],
          "reasoning_effort": "high"
        }

    Example — control-layer call (routing decision, history filter):
        {
          "messages": [
            {"role": "system", "content": "You are the routing layer..."},
            {"role": "user",   "content": "Classify this intent and route appropriately"}
          ],
          "reasoning_effort": "low",
          "model": "nervous_system"
        }
    """
    messages:         List[Message]
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None
    model:            Optional[Literal["base", "nervous_system"]] = None


# ---------------------------------------------------------------------------
# Reasoning effort injection
#
# The model reads the reasoning effort level from the system message.
# The effort is injected as "Reasoning: <level>" appended to the existing
# system message content. If no system message is present, one is created.
# The rest of the messages are passed through unchanged.
# ---------------------------------------------------------------------------
def inject_reasoning_effort(
    messages: List[Message],
    effort: str,
) -> List[dict]:
    """
    Inject the reasoning effort level into the system message.

    Both models read reasoning effort from the system prompt as:
        "Reasoning: low" / "Reasoning: medium" / "Reasoning: high"

    This function appends the instruction to the existing system message so
    openagent-api does not need to manage it manually. The effort level comes
    from the request field if set, otherwise from the REASONING_EFFORT env
    var default.

    Parameters
    ----------
    messages : list of Message objects from the request
    effort   : "low", "medium", or "high"

    Returns
    -------
    list of plain dicts ready for the provider API call
    """
    result = [{"role": m.role, "content": m.content} for m in messages]

    # Find the system message and append the reasoning instruction
    for msg in result:
        if msg["role"] == "system":
            msg["content"] = f"{msg['content']}\nReasoning: {effort}"
            return result

    # No system message found — prepend one with just the reasoning level
    result.insert(0, {"role": "system", "content": f"Reasoning: {effort}"})
    return result


# ---------------------------------------------------------------------------
# SSE proxy stream
#
# Forwards the request to the provider's OpenAI-compatible endpoint and
# streams the response back to the caller as SSE. Uses httpx for async HTTP.
# ---------------------------------------------------------------------------
async def proxy_stream(
    messages: List[Message],
    reasoning_effort: str,
    model: str = "base",
) -> any:
    """
    Async generator that forwards the chat request to the appropriate
    provider endpoint and yields SSE chunks back to the caller as they arrive.

    Routes to the base model by default. Routes to the nervous-system model
    when model="nervous_system" is passed.

    Each line from the provider is forwarded unchanged — no buffering or
    modification. The final [DONE] event is passed through as-is.

    Parameters
    ----------
    messages         : list of Message objects from the request
    reasoning_effort : "low", "medium", or "high"
    model            : "base" (default) or "nervous_system"
    """
    prepared_messages = inject_reasoning_effort(messages, reasoning_effort)

    payload = {
        "messages": prepared_messages,
        "stream":   True,
    }

    # Route to the correct provider endpoint based on the model field.
    # Default is always the base model — the pipeline is unbroken for all
    # callers that do not pass a model field. Each URL is a full
    # OpenAI-compatible chat-completions endpoint; the proxy POSTs to it
    # directly without modifying the path.
    upstream_url = NERVOUS_SYSTEM_URL if model == "nervous_system" else BASE_MODEL_URL

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST",
                upstream_url,
                json=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {PROVIDER_API_KEY}",
                },
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(
                        "Provider returned %d: %s",
                        response.status_code,
                        error_body.decode(errors="replace"),
                    )
                    yield f"data: [ERROR] provider returned {response.status_code}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                async for line in response.aiter_lines():
                    if line:
                        yield f"{line}\n\n"

    except httpx.ConnectError:
        logger.error("Cannot connect to provider at %s", upstream_url)
        yield "data: [ERROR] provider endpoint is not reachable\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        logger.error("Proxy stream error: %s", exc)
        yield f"data: [ERROR] {exc}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Provider reachability probe (for /health)
#
# Each model endpoint is a POST-only chat-completions URL, so a GET returns
# a method-not-allowed (or similar 4xx) when the host is alive — which is all
# we need for a liveness signal. Treat any HTTP response below 500 as
# "reachable"; treat a connection error, timeout, or 5xx (provider erroring
# or a serverless worker still spinning up) as "unreachable".
# ---------------------------------------------------------------------------
async def probe_endpoint(url: str) -> bool:
    """Return True if the provider endpoint answers with a status < 500."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {PROVIDER_API_KEY}"},
            )
            return resp.status_code < 500
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/chat")
async def chat(
    request: ChatRequest,
    api_key: str = Security(verify_api_key),
):
    """
    Inference endpoint — OpenAI messages format, SSE streaming.

    Validates the API key and request body, injects the reasoning effort
    level into the system message, then forwards to the provider and streams
    the response back as Server-Sent Events.

    The caller (openagent-api) is responsible for constructing the full
    messages list including the persona. This endpoint does not add, remove,
    or modify any messages except to append the reasoning level to the system
    message automatically.

    Requires a valid X-API-Key header. Missing or invalid keys receive a 401.

    Example
    -------
    curl -X POST http://localhost:8002/chat \\
         -H "Content-Type: application/json" \\
         -H "X-API-Key: your_api_key_here" \\
         -d '{"messages": [{"role": "system", "content": "You are OpenAgent..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \\
         --no-buffer
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    roles = [m.role for m in request.messages]
    if "user" not in roles:
        raise HTTPException(
            status_code=400,
            detail="Messages must include at least one user message",
        )

    effort     = request.reasoning_effort or REASONING_EFFORT
    model_name = request.model or "base"

    logger.info(
        "POST /chat | messages: %d | reasoning: %s | model: %s | api_key: ...%s",
        len(request.messages),
        effort,
        model_name,
        api_key[-4:] if api_key else "none",
    )

    return StreamingResponse(
        proxy_stream(request.messages, effort, model_name),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    """
    Health check endpoint. No authentication required.

    Checks that the proxy is running and probes both provider endpoints.
    Returns the status of each.

    status is "ok" when the base model endpoint is reachable; the
    nervous-system endpoint is checked independently and does not affect the
    top-level status (it may not be configured yet).
    """
    base_model_ok     = await probe_endpoint(BASE_MODEL_URL)
    nervous_system_ok = await probe_endpoint(NERVOUS_SYSTEM_URL) if NERVOUS_SYSTEM_URL else False

    status = "ok" if base_model_ok else "degraded"
    return {
        "status":         status,
        "proxy":          "ok",
        "base_model":     "ok" if base_model_ok     else "unreachable",
        "nervous_system": "ok" if nervous_system_ok else ("unreachable" if NERVOUS_SYSTEM_URL else "not configured"),
    }