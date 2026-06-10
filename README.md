# openagent-infra

> **OpenAgent model inference infrastructure** — the model serving proxy layer of the OpenAgent system.

---

## Overview

`openagent-infra` is the inference proxy for OpenAgent. This repo is solely responsible for proxying requests to external compute providers, authenticating callers, and returning responses via a small, production-shaped REST API. It exposes two model routes — `POST /chat` (streamed chat, via Server-Sent Events) and `POST /embed` (embeddings, a single JSON response) — and is the single point through which every model in the system is reached.

It is intentionally scoped to the model layer only. It has no knowledge of the OpenAgent persona, the frontend, the conversation state, or the vector store — those live in separate repos. The boundary is clean by design: **openagent-infra proxies the models, everything else builds on top of it.**

---

## The BYOC strategy

`openagent-infra` is deliberately model- and provider-agnostic, built around a **Bring Your Own Compute (BYOC)** approach. It proxies requests to any OpenAI-compatible API endpoint — vLLM workers on RunPod, a local Ollama instance, a standard commercial API, whatever you point it at.

It routes between three logical models:

- **base_model** — the primary reasoning model handling everyday conversations (`/chat`, default).
- **nervous_system** — the fast, lightweight control layer handling routing, history filtering, and agent decisions (`/chat`, `model="nervous_system"`).
- **embedding model** — turns text into vectors for retrieval, e.g. conversation-history search (`/embed`).

Each is configured as a **base** endpoint URL; the proxy appends the OpenAI path (`/v1/chat/completions` or `/v1/embeddings`) when forwarding. Keeping the proxy provider-agnostic means the rest of the stack never has to care where inference actually happens; you can swap providers or model sizes by changing a URL and a key.

---

## Where This Fits

```text
openagent-os
│
├── openagent-infra      ← YOU ARE HERE
│   └── Model proxy API (port 8002)
│       Model proxy layer
│
├── openagent-frontend   ← separate repo
│   └── The product experience (port 8000)
│       Talks to openagent-api
│
├── openagent-api        ← separate repo
│   └── The Identity Gateway (port 8001)
│       Talks to openagent-infra
│
└── openagent-logger     ← separate repo
    └── The capture layer (port 8003)
```

The naming convention is intentional:
- `openagent-infra` handles the **model** connectivity and compute provision.
- `openagent-*` (api, frontend, logger) handle the **product** — gateway, UI, identity, and state.

**Port topology:**
```text
openagent-api (:8001) → openagent-infra (:8002) → BYOC Provider Base Model           [/chat, default]
                                                → BYOC Provider Control Layer Model  [/chat, model="nervous_system"]
                                                → BYOC Provider Embedding Model       [/embed]
```

`openagent-api` is the primary caller of `openagent-infra` today; other server-side services may call it as the system grows (for example, a retrieval layer embedding queries via `/embed`). The proxy is never called directly from a browser.

---

## Architecture

```text
┌───────────────────────────────────────────────────────┐
│                  Docker Container                     │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │          openagent-infra  (port 8002)           │  │
│  │          FastAPI proxy — src/api/main.py        │  │
│  │                                                 │  │
│  │  POST /chat   →  validates X-API-Key            │  │
│  │               →  injects reasoning_effort       │  │
│  │               →  routes by model field          │  │
│  │               →  streams SSE to caller          │  │
│  │  POST /embed  →  validates X-API-Key            │  │
│  │               →  forwards input to embedding    │  │
│  │               →  returns JSON (no streaming)    │  │
│  │  GET  /health →  checks proxy + all three       │  │
│  │                  provider endpoints             │  │
│  │  Auth: X-API-Key required on /chat and /embed   │  │
│  └────────┬─────────────┬──────────────┬───────────┘  │
└───────────┼─────────────┼──────────────┼──────────────┘
            │ /chat        │ /chat        │ /embed
            │ model="base" │ model=       │
            │ (default)    │ "nervous_..."│
            ▼              ▼              ▼
┌────────────────┐ ┌────────────────┐ ┌────────────────────┐
│ BYOC Provider  │ │ BYOC Provider  │ │ BYOC Provider      │
│ Base Model     │ │ Control Layer  │ │ Embedding Model    │
│ e.g. vLLM /    │ │ e.g. vLLM /    │ │ e.g. BGE-M3 on     │
│ OpenAI         │ │ OpenAI         │ │ vLLM / RunPod      │
│ reasoning model│ │ routing,       │ │ text → vectors     │
│ /chat default  │ │ history, ctrl  │ │ /v1/embeddings     │
└────────────────┘ └────────────────┘ └────────────────────┘
```

### Request flow — `/chat`

1. A caller sends `POST /chat` with `X-API-Key`, messages list, optional `reasoning_effort`, and optional `model`.
2. `openagent-infra` validates the API key — returns `401` if missing or invalid.
3. `openagent-infra` injects `Reasoning: <level>` into the system message automatically.
4. `openagent-infra` routes to the correct external endpoint — base model by default, control layer when `model="nervous_system"` — appending `/v1/chat/completions` to the configured base URL.
5. `openagent-infra` forwards via httpx with `Authorization: Bearer PROVIDER_API_KEY`.
6. The BYOC provider generates tokens.
7. Tokens stream back through `openagent-infra` to the caller as SSE events.
8. A final `data: [DONE]` event signals end of stream. (A provider failure mid-stream surfaces as a `data: [ERROR] ...` event followed by `[DONE]`, since the response is already `HTTP 200`.)

### Request flow — `/embed`

1. A caller sends `POST /embed` with `X-API-Key` and `input` (a string or list of strings).
2. `openagent-infra` validates the API key and that `input` is non-empty.
3. If `EMBEDDING_MODEL_URL` is unset, it returns `503` ("not configured"); `/chat` is unaffected.
4. Otherwise it forwards to `EMBEDDING_MODEL_URL` + `/v1/embeddings` with `Authorization: Bearer PROVIDER_API_KEY`.
5. The provider's OpenAI-compatible embeddings JSON is returned to the caller unchanged (no streaming).

### System prompt ownership

The system prompt — the persona — is owned upstream by **openagent-api**. `openagent-api` sends it as the first message in the OpenAI messages list on every `/chat` request. `openagent-infra` injects the reasoning effort level into it automatically before forwarding to the compute provider. `openagent-infra` never stores or inspects the system prompt content. The `/embed` route carries no persona — it forwards raw input only.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Base image | `python:3.12-slim` |
| Model serving | BYOC — any OpenAI-compatible API endpoint |
| API proxy | FastAPI + uvicorn |
| Streaming | SSE via httpx async proxy (`/chat`); single JSON response (`/embed`) |
| Auth | `X-API-Key` header (caller) + `PROVIDER_API_KEY` Bearer (Compute Provider) |
| Containerization | Docker + Docker Compose |

---

## Prerequisites

- **Docker Desktop** installed
- **A compute provider** (e.g., RunPod, OpenAI, local Ollama) serving your endpoints: a base model, and optionally a nervous-system control model and an embedding model.
- **PROVIDER_API_KEY** — an API key with access to all configured compute endpoints.
- **API_KEY** — a secret key shared with `openagent-api` (and any other server-side caller) for request authentication.

No local GPU required unless you are self-hosting your BYOC endpoints locally.

---

## Project Structure

```text
openagent-infra/
├── docker/
│   └── model/
│       └── Dockerfile              # python:3.12-slim — proxy only, no CUDA
├── src/
│   └── api/
│       └── main.py                 # FastAPI proxy — auth, routing, reasoning injection, embeddings
├── docker-compose.yml
├── requirements.txt
├── .env                            # secrets — never commit this
├── .env.example                    # template for .env
├── .dockerignore
├── .gitignore
└── README.md
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/william-mckeon/openagent-infra.git
cd openagent-infra
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in your values. Each model URL is a **base** endpoint (the provider root, without the OpenAI path — the proxy appends `/v1/chat/completions` or `/v1/embeddings`):

```env
API_KEY=your_long_random_secret_key_here
BASE_MODEL_URL=https://your-provider.com/openai
NERVOUS_SYSTEM_URL=https://your-provider.com/openai
EMBEDDING_MODEL_URL=https://your-provider.com/openai
PROVIDER_API_KEY=your_provider_api_key_here
REASONING_EFFORT=medium
# Optional per-route model names — sent in the forwarded payload only when set.
BASE_MODEL_NAME=
NERVOUS_SYSTEM_MODEL_NAME=
EMBEDDING_MODEL_NAME=
```

`NERVOUS_SYSTEM_URL` and `EMBEDDING_MODEL_URL` are optional — if unset, those routes report "not configured" and the base `/chat` path is unaffected.

The `*_MODEL_NAME` vars are optional and empty by default. When set, the proxy adds `"model": <name>` to that route's forwarded payload; when empty, no model field is sent (the original behavior). Routing is always by URL — the names only shape the payload. Set one **only if your endpoint requires an explicit model field**: notably, many embedding runtimes do — BGE-M3 returns `500` without it, so set `EMBEDDING_MODEL_NAME=BAAI/bge-m3` for the embedding route.

Generate a secure `API_KEY` with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Build the image

```bash
docker-compose build --no-cache
```

### 4. Start the API proxy

```bash
docker-compose up -d
```

The API is ready when you see:

```text
=== OpenAgent Inference API Ready — listening on :8002 ===
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8002
```

Startup takes under 10 seconds — the proxy has no model to load.

---

## API Reference

### `POST /chat`

Send a full OpenAI messages list and receive a streamed response via Server-Sent Events. Optionally set the reasoning effort level and model per request.

Requires a valid `X-API-Key` header on every request.

**Request headers:**
```text
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Request body:**
```json
{
  "messages": [
    {"role": "system",    "content": "You are OpenAgent..."},
    {"role": "user",      "content": "hello"}
  ],
  "reasoning_effort": "medium",
  "model": "base"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | array | Yes | Full OpenAI messages list including system prompt |
| `reasoning_effort` | string | No | `low`, `medium`, or `high`. Defaults to `medium`. |
| `model` | string | No | `base` (default) or `nervous_system`. Routes to the base endpoint or the control layer endpoint. |

**Error responses:**
- `400` — messages list is empty or contains no user message
- `401` — API key missing or invalid
- `422` — request body malformed

Provider-side failures do **not** surface as an HTTP error: once the stream begins the response is already `HTTP 200`, so an unreachable provider or non-200 is reported as an in-stream `data: [ERROR] ...` event followed by `data: [DONE]`. Watch the stream, not just the status code.

**curl:**
```bash
curl -X POST http://localhost:8002/chat \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"messages": [{"role": "system", "content": "You are OpenAgent..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \
     --no-buffer
```

---

### `POST /embed`

Send one or more strings and receive the provider's OpenAI-compatible embeddings JSON as a single response (no streaming). Used to turn text into vectors — e.g. embedding conversation turns for storage in a vector database, and embedding a query at retrieval time.

Requires a valid `X-API-Key` header on every request.

**Request headers:**
```text
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Request body** (single string, or a list to batch):
```json
{ "input": ["first chunk", "second chunk"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input` | string or array of strings | Yes | Text to embed. A list is embedded in one provider call (batch). Cannot be empty. |

The caller sends no `model` field (the route is selected by `EMBEDDING_MODEL_URL`) and no `reasoning_effort` (the embedding model does not reason). The proxy itself adds a `"model"` to the provider payload when the server-side `EMBEDDING_MODEL_NAME` is set — required by runtimes like BGE-M3 — but that is configuration, not a caller field.

**Response:** the provider's embeddings JSON, passed through unchanged:
```json
{
  "object": "list",
  "data": [ { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, "..."] } ],
  "model": "<provider model id>",
  "usage": { "prompt_tokens": 7, "total_tokens": 7 }
}
```

**Error responses:**
- `400` — `input` is empty
- `401` — API key missing or invalid
- `422` — request body malformed
- `502` — embedding provider returned a non-200, or a proxy error
- `503` — `EMBEDDING_MODEL_URL` not set, or the embedding provider is not reachable

**curl:**
```bash
curl -X POST http://localhost:8002/embed \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"input": ["first chunk", "second chunk"]}'
```

---

### `GET /health`

Health check. No authentication required. Probes the proxy and all three compute endpoints independently.

**Fully ready:**
```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "ok", "embedding": "ok"}
```

**Base provider host unreachable:**
```json
{"status": "degraded", "proxy": "ok", "base_model": "unreachable", "nervous_system": "ok", "embedding": "ok"}
```

**Nervous-system / embedding not yet configured:**
```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "not configured", "embedding": "not configured"}
```

`status` is `ok` when the base provider **host** is reachable, and `degraded` only when it is not. `/health` answers *"is the host reachable?"*, not *"is the model warm?"* — a reachable host whose worker is cold (scale-to-zero, still spinning up) reports `ok`, and that cold start is absorbed on the first `/chat` or `/embed`. `nervous_system` and `embedding` are checked independently and do not change the top-level status; `not configured` means the corresponding URL is unset in `.env`.

```bash
curl http://localhost:8002/health
```

---

### `GET /docs`

Auto-generated Swagger UI:

```text
http://localhost:8002/docs
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | — | Secret key for X-API-Key auth on `/chat` and `/embed` (required) |
| `BASE_MODEL_URL` | — | **Base** endpoint for the primary model (no path; proxy appends `/v1/chat/completions`). Default for all `/chat` requests (required) |
| `NERVOUS_SYSTEM_URL` | — | **Base** endpoint for the fast control model. Used when `model="nervous_system"`. Optional |
| `EMBEDDING_MODEL_URL` | — | **Base** endpoint for the embedding model (no path; proxy appends `/v1/embeddings`). Used by `POST /embed`. Optional |
| `PROVIDER_API_KEY` | — | API key for Bearer auth on all compute endpoints (required) |
| `REASONING_EFFORT` | `medium` | Default reasoning level for the chat models — `low`, `medium`, or `high` |
| `BASE_MODEL_NAME` | `""` | Optional. When set, the proxy adds `"model": <name>` to the `model="base"` `/chat` payload; empty by default |
| `NERVOUS_SYSTEM_MODEL_NAME` | `""` | Optional. Same as `BASE_MODEL_NAME`, for `model="nervous_system"` `/chat` requests |
| `EMBEDDING_MODEL_NAME` | `""` | Optional. When set, the proxy adds `"model": <name>` to the `/embed` payload. Required by runtimes that demand it — BGE-M3 returns `500` without it (set `BAAI/bge-m3`) |

---

## Design Decisions

### Why keep the FastAPI proxy layer instead of calling providers directly?

Compute providers usually support only a single shared API key. By keeping FastAPI as a proxy, auth validation lives in one place and can be swapped or managed without exposing the provider's billing API key to the gateway or frontend layers. It is also the single chokepoint every model path runs through — chat and embeddings alike — so no other service holds `PROVIDER_API_KEY`.

### Why a separate `/embed` endpoint instead of a `model` route on `/chat`?

`/chat` is welded to the messages-in / SSE-stream-out contract. Embeddings have a different request shape (raw input strings), a different response (a single JSON vector array, no streaming), a different upstream path (`/v1/embeddings`), and no reasoning. They get their own endpoint while reusing the same auth, `PROVIDER_API_KEY`, and base-URL convention. (Because `/embed` is non-streaming, it returns real HTTP error codes; `/chat` can only report provider failures as an in-stream `[ERROR]` event, since its `200` is already committed.)

### Why two separate API keys?

`API_KEY` is the key a caller sends to `openagent-infra` — it identifies and authenticates the caller. `PROVIDER_API_KEY` is the key `openagent-infra` sends to the compute provider — it authenticates `openagent-infra` to the inference backend. These concerns are deliberately separated so the caller key can be rotated without affecting the provider configuration, and vice versa. The caller-side validation is isolated so it can later move from a single shared key to per-caller keys.

### Why reasoning effort as an API parameter?

Both chat models support configurable reasoning effort — low, medium, high. It's a genuine feature: a frontend can expose it as Quick / Standard / Deep mode, and tooling can set it per use case programmatically. The proxy injects it into the system message automatically.

### Why OpenAI messages / embeddings format?

The OpenAI formats make `openagent-infra` compatible with almost any frontend and model backend on the market. They keep the serving layer stateless and the protocol standard.

### Why port 8002?

Port 8000 is reserved for openagent-frontend. Port 8001 is reserved for openagent-api. Port 8002 is the exposed port for openagent-infra.

---

## License

Copyright © 2026 William McKeon.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

```text
http://www.apache.org/licenses/LICENSE-2.0
```

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

---

## Maintainer

**William McKeon** ([github.com/william-mckeon](https://github.com/william-mckeon))