"""
src/api/main.py

OpenAgent inference API — thin FastAPI proxy layer.

This file is the public-facing API for openagent-infra. It sits between its
server-side callers (openagent-api today, and potentially other internal
services as the system grows) and one or more external compute providers,
handling authentication and request validation before forwarding. For chat it
streams the provider's response back as Server-Sent Events; for embeddings it
forwards a single JSON response.

Every path to a model in the OpenAgent system goes through this proxy — chat
models and the embedding model alike. Nothing talks to a provider endpoint
directly; this service is the single chokepoint that holds PROVIDER_API_KEY
and presents a stable, OpenAI-shaped contract behind which the BYOC provider
can be swapped.

Architecture
---------------------------------------
        openagent-api  (the primary caller today; other internal,
                        server-side services may call it as the system grows)
                      │
          ┌───────────┴────────────┐
          │                        │
   POST /chat                 POST /embed
   X-API-Key, messages,       X-API-Key, input
   reasoning_effort, model?   (string or list of strings)
          │                        │
          ▼                        ▼
              src/api/main.py          ← YOU ARE HERE
              FastAPI proxy (port 8002)
              - Validates X-API-Key on every /chat and /embed request
              - Validates the request body
              - /chat  : injects reasoning_effort, routes by `model`,
                         streams the SSE response back byte-for-byte
              - /embed : forwards input to the embedding model and returns
                         the provider's JSON response (no streaming)
          │                        │
          ▼                        ▼
   POST <BASE_MODEL_URL |     POST <EMBEDDING_MODEL_URL>
        NERVOUS_SYSTEM_URL>        + /v1/embeddings
        + /v1/chat/completions
                      │
                      ▼
              BYOC Compute Provider    (any OpenAI-compatible endpoint)
              - model="base" (default)  → BASE_MODEL_URL      (base reasoning model)
              - model="nervous_system"  → NERVOUS_SYSTEM_URL  (fast control model)
              - POST /embed             → EMBEDDING_MODEL_URL  (embedding model)
              - the proxy appends the OpenAI path to the configured BASE url
              - chat : /v1/chat/completions, SSE streaming
              - embed: /v1/embeddings, single JSON response
              - an optional per-route model name (BASE_MODEL_NAME,
                NERVOUS_SYSTEM_MODEL_NAME, EMBEDDING_MODEL_NAME) is added to the
                forwarded payload only when set — required by some providers
                (e.g. a BGE-M3 embedding server), omitted otherwise

Why a proxy layer instead of exposing the provider directly
---------------------------------------
A provider usually offers a single shared API key. Keeping FastAPI as a proxy
puts caller authentication in one place and keeps the provider's billing
credential out of the gateway and frontend layers. It also gives a stable,
OpenAI-shaped contract regardless of which provider sits behind it. The
validation logic is isolated in verify_api_key so it can later be swapped
from a static .env key to a per-caller lookup without touching the endpoint
contract or the provider configuration.

System prompt ownership
---------------------------------------
The persona is owned by openagent-api, not openagent-infra. openagent-api
sends it as the first system message in the messages list on every /chat
request. This file does not add, modify, or inspect any messages — it forwards
the full list to the provider exactly as received, with the reasoning_effort
instruction injected into the existing system message automatically. The
embedding route carries no persona: /embed forwards raw input strings only.

Reasoning effort
---------------------------------------
Both the base model and the nervous-system model support configurable
reasoning effort — low, medium, high. The effort level is injected into the
system message before forwarding. Default is medium. openagent-api or any
tooling can set it per request based on the complexity of the task:
  - low    : fast responses, lightweight tooling calls, simple queries
  - medium : standard interactions (default)
  - high   : complex reasoning, deep analysis, multi-step tasks
Reasoning effort applies to the two chat models only; the embedding model
does not reason.

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

POST /embed
    Header       : X-API-Key: <your_api_key>
    Request body : { "input": "text"  OR  ["text one", "text two", ...] }
    Response     : application/json — the provider's OpenAI-compatible
                   embeddings response, passed through unchanged:
                   { "object": "list", "data": [ { "embedding": [...], ... } ], ... }

GET /health
    Returns {"status": "ok" | "degraded", ...} — no auth required.
    Checks the proxy and all configured provider endpoints (base,
    nervous-system, embedding).

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

    curl -X POST http://localhost:8002/embed \\
         -H "Content-Type: application/json" \\
         -H "X-API-Key: your_api_key_here" \\
         -d '{"input": ["first chunk", "second chunk"]}'

    curl http://localhost:8002/health
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import List, Literal, Optional, Union

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse, StreamingResponse
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
# API_KEY             : Secret validated against the X-API-Key header on /chat
#                       and /embed. The same value openagent-api holds as
#                       INFRA_API_KEY. Isolated in verify_api_key for a future
#                       per-caller swap (multiple server-side callers may share
#                       this key today; per-caller keys are the later path).
# BASE_MODEL_URL      : OpenAI-compatible BASE endpoint for the base reasoning
#                       model — the provider root, WITHOUT the chat-completions
#                       path. Default route for all /chat requests (model="base"
#                       or no model field). The proxy appends /v1/chat/completions
#                       when forwarding, and /health probes this bare base URL
#                       directly. e.g. https://your-provider.com/openai
# NERVOUS_SYSTEM_URL  : OpenAI-compatible BASE endpoint for the fast control
#                       model. Used when model="nervous_system". Same base form
#                       as BASE_MODEL_URL. Optional — when unset, that route is
#                       "not configured".
# EMBEDDING_MODEL_URL : OpenAI-compatible BASE endpoint for the embedding
#                       model. Used by POST /embed. Same base form as
#                       BASE_MODEL_URL; the proxy appends /v1/embeddings when
#                       forwarding. Optional — when unset, /embed returns a
#                       clear "not configured" error and /chat is unaffected.
# PROVIDER_API_KEY    : Sent as Authorization: Bearer on every forwarded
#                       request to the provider endpoint(s). Never exposed
#                       to any caller. Required.
# REASONING_EFFORT    : Default reasoning effort if not set per request.
#                       Applies to both chat models (the embedding model does
#                       not reason). low | medium (default) | high.
#
# BASE_MODEL_NAME           : Optional model name for the base route. When set,
#                             the proxy adds "model": <name> to the payload it
#                             forwards for model="base" /chat requests. When
#                             empty, no model field is sent (original behavior).
# NERVOUS_SYSTEM_MODEL_NAME : Optional model name for the nervous-system route.
#                             Same behavior, for model="nervous_system" /chat
#                             requests.
# EMBEDDING_MODEL_NAME      : Optional model name for the embedding route. When
#                             set, the proxy adds "model": <name> to the /embed
#                             payload. Some embedding runtimes (e.g. a BGE-M3
#                             server) REQUIRE this field and return 500 without
#                             it; set it to the served model id, e.g. BAAI/bge-m3.
#
# Model name is OPTIONAL PER ROUTE. Routing is still by URL selection — the
# *_MODEL_NAME vars do not change which endpoint a request goes to. They only
# control whether a "model" field is included in the forwarded payload:
#   - var set   -> the proxy adds "model": <name> to that route's payload
#   - var empty -> the proxy omits "model" entirely (the original behavior)
# This keeps providers whose endpoint serves a fixed model working untouched
# (leave the var empty), while satisfying providers that require an explicit
# model field (set it). It holds independently for /chat (base, nervous-system)
# and /embed.
# ---------------------------------------------------------------------------
API_KEY             = os.environ.get("API_KEY", "")
BASE_MODEL_URL      = os.environ.get("BASE_MODEL_URL", "")
NERVOUS_SYSTEM_URL  = os.environ.get("NERVOUS_SYSTEM_URL", "")
EMBEDDING_MODEL_URL = os.environ.get("EMBEDDING_MODEL_URL", "")
PROVIDER_API_KEY    = os.environ.get("PROVIDER_API_KEY", "")
REASONING_EFFORT    = os.environ.get("REASONING_EFFORT", "medium")

# Optional per-route model names (see note above). Empty by default — when set,
# the proxy includes "model": <name> in that route's forwarded payload.
BASE_MODEL_NAME           = os.environ.get("BASE_MODEL_NAME", "")
NERVOUS_SYSTEM_MODEL_NAME = os.environ.get("NERVOUS_SYSTEM_MODEL_NAME", "")
EMBEDDING_MODEL_NAME      = os.environ.get("EMBEDDING_MODEL_NAME", "")


# ---------------------------------------------------------------------------
# API key authentication
#
# Every /chat and /embed request must include a valid X-API-Key header.
# Requests with missing or invalid keys receive 401 Unauthorized.
#
# Design note: the validation logic is isolated in verify_api_key so it can
# be swapped from a static .env check to a per-caller lookup without touching
# the endpoint logic. This is the seam for supporting multiple server-side
# callers with independent keys later.
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Security(api_key_header)) -> str:
    """
    FastAPI dependency that validates the X-API-Key header.

    Raises 401 if the header is missing or the key does not match API_KEY.
    Returns the key on success. Shared by /chat and /embed.
    """
    if not key or key != API_KEY:
        logger.warning("Unauthorized request — invalid or missing API key")
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
    logger.info("Embedding model URL   : %s", EMBEDDING_MODEL_URL or "NOT SET")
    logger.info("Base model name       : %s", BASE_MODEL_NAME or "(not sent)")
    logger.info("Nervous system name   : %s", NERVOUS_SYSTEM_MODEL_NAME or "(not sent)")
    logger.info("Embedding model name  : %s", EMBEDDING_MODEL_NAME or "(not sent)")
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
# Request schemas
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


class EmbedRequest(BaseModel):
    """
    Request body for POST /embed.

    input : The text to embed. Either a single string or a list of strings.
            A list is embedded in a single provider call (batch) — preferred
            when embedding several items at once (e.g. multiple conversation
            chunks at write time), since it avoids one round-trip per item.

            The caller sends no `model` field — the embedding route is
            selected by URL (EMBEDDING_MODEL_URL), the same way /chat selects an
            endpoint by URL rather than by a model name in the body. The proxy
            itself adds a "model" to the *provider* payload when the server-side
            EMBEDDING_MODEL_NAME is set (some embedding runtimes require it);
            that is configuration, not a caller field.

    Example — single string:
        { "input": "the quick brown fox" }

    Example — batch:
        { "input": ["first chunk", "second chunk", "third chunk"] }
    """
    input: Union[str, List[str]]


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

    Both chat models read reasoning effort from the system prompt as:
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
# SSE proxy stream (for /chat)
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
    # callers that do not pass a model field. Each configured URL is a BASE
    # endpoint; the proxy appends the OpenAI chat-completions path here when
    # forwarding. /health probes the bare base URL, so a health check never
    # touches the inference route (and never wakes a scale-to-zero worker).
    #
    # The matching per-route model name (BASE_MODEL_NAME / NERVOUS_SYSTEM_MODEL_NAME)
    # is selected alongside the URL. Routing is by URL; the name only decides
    # whether a "model" field rides along in the payload (see below).
    if model == "nervous_system":
        upstream_base = NERVOUS_SYSTEM_URL
        model_name    = NERVOUS_SYSTEM_MODEL_NAME
    else:
        upstream_base = BASE_MODEL_URL
        model_name    = BASE_MODEL_NAME

    upstream_url = upstream_base.rstrip("/") + "/v1/chat/completions"

    # Include "model" only when this route's name is configured. When empty,
    # the field is omitted — the original behavior, preserved for providers
    # whose endpoint serves a fixed model and needs no model field.
    if model_name:
        payload["model"] = model_name

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
# Embedding proxy (for /embed) — non-streaming
#
# Forwards an /embed request to the provider's OpenAI-compatible embeddings
# endpoint and returns the provider's response. Unlike /chat this is NOT a
# stream — embeddings come back as a single JSON body, so the proxy makes one
# POST and hands the whole response back to the endpoint, which passes the
# JSON through unchanged.
#
# The configured EMBEDDING_MODEL_URL is a BASE endpoint (same convention as
# BASE_MODEL_URL / NERVOUS_SYSTEM_URL); the proxy appends /v1/embeddings here.
#
# Model name: when EMBEDDING_MODEL_NAME is set, the proxy adds "model": <name>
# to the forwarded payload. Some embedding runtimes (e.g. a BGE-M3 server)
# require this field and return 500 without it; others ignore it. When the var
# is empty, no model field is sent (the original behavior).
#
# Timeout matches /chat (generous, not short) on purpose: a scale-to-zero
# embedding worker still cold-starts on its first call after an idle period,
# even though a warm embed returns in milliseconds. A short read timeout would
# abort that cold start. The cold start is absorbed here at call time, exactly
# as it is for /chat.
#
# Note: client.post() reads the full response body before returning, so the
# returned Response is safe to read (.json()/.text) after the client closes.
# ---------------------------------------------------------------------------
async def proxy_embed(inputs: Union[str, List[str]]) -> httpx.Response:
    """
    Forward the embed request to the embedding provider and return the raw
    httpx.Response. The caller (the /embed endpoint) handles status and body.

    Parameters
    ----------
    inputs : a single string or a list of strings to embed

    Returns
    -------
    httpx.Response from the provider's /v1/embeddings endpoint
    """
    upstream_url = EMBEDDING_MODEL_URL.rstrip("/") + "/v1/embeddings"

    payload = {"input": inputs}

    # Include "model" only when EMBEDDING_MODEL_NAME is configured. Required by
    # some embedding runtimes (e.g. BGE-M3, which returns 500 without it);
    # omitted when empty, preserving the original no-model-field behavior.
    if EMBEDDING_MODEL_NAME:
        payload["model"] = EMBEDDING_MODEL_NAME

    async with httpx.AsyncClient(timeout=600.0) as client:
        return await client.post(
            upstream_url,
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {PROVIDER_API_KEY}",
            },
        )


# ---------------------------------------------------------------------------
# Provider reachability probe (for /health)
#
# Probes the lightweight BASE provider URL — NOT the inference route — so a
# health check never wakes a scale-to-zero worker and never mistakes a cold
# model for a dead host. The question is "is the provider HOST reachable?",
# not "is the model warm?":
#   - any response below 500                       → reachable
#   - connection refused / connect-timeout / 5xx   → unreachable
#   - a slow read (a cold/scale-to-zero worker still spinning up) → STILL
#     reachable; the host answered the connection, the model is just warming,
#     and that cold start is absorbed at chat/embed time (where the read
#     timeout is unbounded). A cold model must report reachable, not
#     unreachable, or the whole chain (api /health, then the frontend gate)
#     stalls waiting on it.
# Short, separate connect/read timeouts keep /health fast and non-blocking.
# Reused unchanged for all three endpoints (base, nervous-system, embedding).
# ---------------------------------------------------------------------------
async def probe_endpoint(url: str) -> bool:
    """Return True if the provider host is reachable (warm OR cold)."""
    if not url:
        return False
    timeout = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {PROVIDER_API_KEY}"},
            )
            return resp.status_code < 500
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # The host itself could not be reached.
        return False
    except httpx.TimeoutException:
        # Connected, but slow to respond — a cold worker spinning up. The host
        # is reachable; the model is just warming.
        return True
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


@app.post("/embed")
async def embed(
    request: EmbedRequest,
    api_key: str = Security(verify_api_key),
):
    """
    Embedding endpoint — OpenAI embeddings format, single JSON response.

    Validates the API key and input, then forwards the text to the embedding
    provider and returns the provider's OpenAI-compatible embeddings JSON,
    passed through unchanged.

    Unlike /chat, this endpoint does not stream and does not involve a persona
    or reasoning effort — it forwards raw input strings only. A list input is
    embedded as a batch in a single provider call.

    Requires a valid X-API-Key header. Missing or invalid keys receive a 401.

    Responses
    ---------
    200 : the provider's embeddings JSON ({"object":"list","data":[...],...})
    400 : input is empty
    401 : missing or invalid X-API-Key
    502 : the embedding provider returned a non-200 status, or an unexpected
          proxy error occurred
    503 : embedding model not configured (EMBEDDING_MODEL_URL unset), or the
          embedding provider is not reachable

    Example
    -------
    curl -X POST http://localhost:8002/embed \\
         -H "Content-Type: application/json" \\
         -H "X-API-Key: your_api_key_here" \\
         -d '{"input": ["first chunk", "second chunk"]}'
    """
    # Validate that input is present and non-empty.
    if isinstance(request.input, str):
        if not request.input.strip():
            raise HTTPException(status_code=400, detail="Input cannot be empty")
        input_count = 1
    else:
        if len(request.input) == 0:
            raise HTTPException(status_code=400, detail="Input list cannot be empty")
        input_count = len(request.input)

    # Graceful "not configured" — the embedding route is optional, like the
    # nervous-system route. /chat is entirely unaffected when this is unset.
    if not EMBEDDING_MODEL_URL:
        logger.warning("POST /embed called but EMBEDDING_MODEL_URL is not set")
        raise HTTPException(
            status_code=503,
            detail="Embedding model not configured",
        )

    logger.info(
        "POST /embed | inputs: %d | api_key: ...%s",
        input_count,
        api_key[-4:] if api_key else "none",
    )

    try:
        response = await proxy_embed(request.input)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.error("Cannot connect to embedding provider")
        raise HTTPException(
            status_code=503,
            detail="Embedding provider is not reachable",
        )
    except Exception as exc:
        logger.error("Embedding proxy error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Embedding proxy error: {exc}")

    # Forward a non-200 from the provider as a 502 (the upstream status is
    # logged for the operator). A streaming /chat error can only surface as an
    # SSE event because the 200 stream has already begun; /embed is a single
    # response, so it can return a real error status here.
    if response.status_code != 200:
        logger.error(
            "Embedding provider returned %d: %s",
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Embedding provider returned {response.status_code}",
        )

    # Passthrough — return the provider's OpenAI-compatible embeddings JSON
    # exactly as received.
    try:
        data = response.json()
    except Exception as exc:
        logger.error("Embedding provider returned a non-JSON 200 body: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Embedding provider returned an invalid response",
        )

    return JSONResponse(status_code=200, content=data)


@app.get("/health")
async def health():
    """
    Health check endpoint. No authentication required.

    Checks that the proxy is running and probes all three provider endpoints
    (base, nervous-system, embedding). Returns the status of each.

    status is "ok" when the base provider is reachable — including when its
    model worker is cold/scale-to-zero (a reachable-but-warming provider is
    "ok", not "unreachable"; the cold start is absorbed at chat/embed time).
    The nervous-system and embedding endpoints are checked independently and
    do NOT affect the top-level status (either may be unconfigured). All three
    probes run concurrently so /health stays fast.
    """
    base_model_ok, nervous_system_ok, embedding_ok = await asyncio.gather(
        probe_endpoint(BASE_MODEL_URL),
        probe_endpoint(NERVOUS_SYSTEM_URL),
        probe_endpoint(EMBEDDING_MODEL_URL),
    )

    status = "ok" if base_model_ok else "degraded"
    return {
        "status":         status,
        "proxy":          "ok",
        "base_model":     "ok" if base_model_ok     else "unreachable",
        "nervous_system": "ok" if nervous_system_ok else ("unreachable" if NERVOUS_SYSTEM_URL else "not configured"),
        "embedding":      "ok" if embedding_ok      else ("unreachable" if EMBEDDING_MODEL_URL else "not configured"),
    }