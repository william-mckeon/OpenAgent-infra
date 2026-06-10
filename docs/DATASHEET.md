# openagent-infra — Datasheet

> Reference document for building on top of openagent-infra.
> Intended audience: **openagent-api** (the primary caller today) and any
> other server-side service that needs to understand what openagent-infra is,
> what it owns, and how it is called.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Model inference proxy for the OpenAgent system |
| Base URL | `http://localhost:8002` |
| Protocol | HTTP/1.1 |
| Streaming | SSE (`/chat`) · single JSON response (`/embed`) |
| Auth in (caller) | `X-API-Key` header (required on `/chat` and `/embed`) |
| Auth out (provider) | `Authorization: Bearer PROVIDER_API_KEY` |
| Content type in | `application/json` |
| Content type out | `text/event-stream` (`/chat`) · `application/json` (`/embed`) |
| Request format | OpenAI messages (`/chat`) · OpenAI embeddings input (`/embed`) |
| Reasoning effort | `low` / `medium` / `high` (optional `/chat` field, default: `medium`) |
| Model selection | `base` (default) / `nervous_system` (optional `/chat` field) |
| Chat endpoint | `POST /chat` |
| Embed endpoint | `POST /embed` |
| Health endpoint | `GET /health` |
| Docs UI | `GET /docs` |
| Primary caller | openagent-api (other server-side callers possible as the system grows) |
| Backend | BYOC provider — any OpenAI-compatible endpoint(s) |
| Version | 1.0.0 |

---

## Overview

`openagent-infra` is the **model inference proxy** of the OpenAgent system. It sits between its server-side callers (openagent-api today, and potentially other internal services as the system grows) and one or more external compute providers. It is the single point through which every model in the system is reached — chat models and the embedding model alike.

It does two jobs:

- **Chat (`POST /chat`)** — authenticates the caller, injects the per-request reasoning effort level into the system message, routes to the base or nervous-system model, and streams the provider's response back as Server-Sent Events.
- **Embeddings (`POST /embed`)** — authenticates the caller, forwards the input to the embedding model, and returns the provider's OpenAI-compatible embeddings JSON as a single response (no streaming).

It is deliberately model- and provider-agnostic, built around a **Bring Your Own Compute (BYOC)** approach — it proxies requests to any OpenAI-compatible API endpoint, whether that is a vLLM worker on a serverless host, a local runtime, or a commercial API. The rest of the stack never has to know where inference actually happens.

It is intentionally scoped to the model layer only. It has no knowledge of the OpenAgent persona, the frontend, the conversation state, the vector store, or the capture layer. The boundary is clean by design: **openagent-infra proxies the models; everything else builds on top of it.**

---

## Where This Service Fits

```text
┌──────────────────────────────────────────────────────────────┐
│    Callers  (server-side, separate repos / Docker stacks)    │
│                                                              │
│    openagent-api on :8001 is the primary caller today. Other │
│    internal services may call the proxy as the system grows  │
│    (e.g. a retrieval layer embedding queries via /embed).    │
│    api owns the persona, the auth chain, and the SSE relay,  │
│    and constructs the full messages list (system prompt      │
│    first) for /chat.                                         │
└───────────────────────────┬──────────────────────────────────┘
              │ POST /chat   (SSE response)    X-API-Key: <API_KEY>
              │ POST /embed  (JSON response)   X-API-Key: <API_KEY>
              │ GET  /health (no auth)
              ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-infra   ←── YOU ARE READING THIS DATASHEET      │
│    FastAPI proxy on :8002                                    │
│                                                              │
│    Owns: caller auth, reasoning-effort injection (chat),     │
│          model routing (base / nervous_system / embedding),  │
│          PROVIDER_API_KEY, byte-for-byte SSE relay (chat),   │
│          JSON passthrough (embed)                            │
│    Stateless — the full request is sent on every call        │
└──────┬───────────────────────┬───────────────────────┬───────┘
       │ /chat model="base"     │ /chat                  │ /embed
       │ (default)              │ model="nervous_system" │
       ▼                        ▼                        ▼
┌────────────────────┐ ┌──────────────────────┐ ┌────────────────────┐
│ BYOC Provider      │ │ BYOC Provider        │ │ BYOC Provider      │
│ Base Model         │ │ Control Layer        │ │ Embedding Model    │
│ (BASE_MODEL_URL)   │ │ (NERVOUS_SYSTEM_URL) │ │ (EMBEDDING_MODEL_  │
│ reasoning model    │ │ fast control model:  │ │  URL)              │
│ all /chat default  │ │ routing, history     │ │ text → vectors     │
│                    │ │ filtering, decisions │ │ used by /embed     │
│ scales to zero     │ │ scales to zero       │ │ scales to zero     │
│ (if serverless)    │ │ (if serverless)      │ │ (if serverless)    │
└────────────────────┘ └──────────────────────┘ └────────────────────┘
   /v1/chat/completions    /v1/chat/completions      /v1/embeddings
```

**Port topology:**
```text
openagent-api (:8001) → openagent-infra (:8002) → BYOC Base Model           [/chat, default]
                                                 → BYOC Control Layer Model  [/chat, model="nervous_system"]
                                                 → BYOC Embedding Model      [/embed]
```

`openagent-infra` is reached only by server-side callers — never directly from a browser. It never sees `OPENAGENT_API_KEY` (the frontend↔api secret) or any conversation-capture data. Every model path in the system runs through it; nothing talks to a provider endpoint directly.

---

## Authentication

Every `POST /chat` and `POST /embed` request must include a valid API key in the `X-API-Key` header.

```text
X-API-Key: your_api_key_here
```

Requests with a missing or invalid key receive `401 Unauthorized`. The `/health` endpoint does not require authentication.

The API key is a shared secret between `openagent-infra` and its caller, set via `API_KEY` in `openagent-infra`'s `.env`. **It is the same value `openagent-api` holds as `INFRA_API_KEY`** — the naming differs across the boundary (the api side calls it `INFRA_API_KEY`, the infra side calls it `API_KEY`) but the value must match byte-for-byte. If more than one server-side service calls the proxy, they share this key today; per-caller keys are a future evolution (the validation is isolated in one place — `verify_api_key` — precisely so it can move from a static `.env` key to a per-caller lookup without changing the endpoint contract).

`openagent-infra` uses a second key internally — `PROVIDER_API_KEY` — to authenticate forwarded requests to the BYOC provider endpoint(s) (base model, and when configured, the control-layer and embedding models). This key is never exposed to any caller. Two independent secrets at two independent boundaries: `API_KEY` gates who may call `openagent-infra`; `PROVIDER_API_KEY` authenticates `openagent-infra` to the inference backend. Either can be rotated without touching the other.

---

## API Reference

### `POST /chat`

The chat inference endpoint. Send a full OpenAI messages list and receive a token-by-token streamed response via SSE. Optionally control the reasoning effort level and select the model per request.

#### Request

```text
POST /chat
Content-Type: application/json
X-API-Key: your_api_key_here
```

```json
{
  "messages": [
    {"role": "system",    "content": "<persona, prepended by openagent-api>"},
    {"role": "user",      "content": "hello"}
  ],
  "reasoning_effort": "medium"
}
```

**With reasoning_effort set to high:**
```json
{
  "messages": [
    {"role": "system", "content": "<persona>"},
    {"role": "user",   "content": "Analyze the tradeoffs between SSE and WebSockets for a streaming chat application"}
  ],
  "reasoning_effort": "high"
}
```

**Multi-turn example:**
```json
{
  "messages": [
    {"role": "system",    "content": "<persona>"},
    {"role": "user",      "content": "What is the Fibonacci sequence?"},
    {"role": "assistant", "content": "The Fibonacci sequence is..."},
    {"role": "user",      "content": "Can you show me in Python?"}
  ],
  "reasoning_effort": "medium"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `messages` | array | Yes | Full OpenAI messages list. Cannot be empty. Must contain at least one `user` message. |
| `messages[].role` | string | Yes | One of `system`, `user`, `assistant`. |
| `messages[].content` | string | Yes | The message content. |
| `reasoning_effort` | string | No | `low`, `medium`, or `high`. Defaults to the server `REASONING_EFFORT` env var (medium). |
| `model` | string | No | `base` (default) or `nervous_system`. Routes to the base reasoning model or the control-layer model. Omitting the field always routes to the base model. |

**Important:** the caller is responsible for constructing the full messages list, including the persona as the first `system` message. `openagent-infra` injects `Reasoning: <level>` into that system message automatically before forwarding to the provider — the caller does not need to manage the reasoning instruction. This applies to both the base model and the control-layer model.

#### Reasoning effort guidance

| Level | Latency | Use for |
|---|---|---|
| `low` | Fastest | Lightweight tooling calls, simple lookups, routing decisions |
| `medium` | Balanced | Standard interactions, general questions (default) |
| `high` | Slowest | Complex analysis, multi-step reasoning, hard problems |

#### Response

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Transfer-Encoding: chunked
X-Accel-Buffering: no
```

Each SSE event payload is a JSON-encoded OpenAI ChatCompletion chunk (the format an OpenAI-compatible provider emits natively) — NOT plain text tokens. Chain-of-thought tokens stream first inside `choices[0].delta.reasoning`, then visible answer tokens inside `choices[0].delta.content`, then a final empty-delta chunk with `finish_reason: "stop"`, then the `[DONE]` sentinel.

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning":"User"},"finish_reason":null}]}

...  (more reasoning tokens — chain-of-thought)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

...  (more content tokens — visible answer)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

`openagent-infra` forwards the provider's SSE stream **byte-for-byte** — it does not decode, re-encode, or interpret the chunk JSON on the relay path. Whatever the OpenAI-compatible provider emits is what the caller receives, terminating with the `[DONE]` sentinel.

**Important:** The stream always ends with `data: [DONE]`. The caller must watch for this event to know generation is complete.

**Important:** Both the base model and the control-layer model are reasoning models — each emits a reasoning chain in `delta.reasoning` before the final answer in `delta.content`. Display or filter the reasoning per the caller's UX choice; see the SSE Stream Specification section below.

#### Error responses

| Status | Condition | Body |
|---|---|---|
| `400` | Messages list is empty or contains no user message | `{"detail": "Messages list cannot be empty"}` |
| `401` | X-API-Key header missing or invalid | `{"detail": "Invalid or missing API key"}` |
| `422` | Request body malformed or missing | FastAPI validation error JSON |

**Provider-side failures on `/chat` are not HTTP errors.** Once the SSE stream has begun the response is already `HTTP 200`, so a provider that is unreachable or returns a non-200 cannot be reported as an HTTP status. Instead it surfaces as an in-stream event — `data: [ERROR] ...` followed by `data: [DONE]`. The caller must watch the stream for an `[ERROR]` payload, not only the HTTP status. (The `/embed` endpoint, being a single non-streaming response, *does* return real error status codes — see below.)

#### Example — curl (testing only)

```bash
curl -X POST http://localhost:8002/chat \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "hello"}], "reasoning_effort": "medium"}' \
     --no-buffer
```

---

### `POST /embed`

The embedding endpoint. Send one or more strings and receive the provider's OpenAI-compatible embeddings response as a single JSON body — no streaming. Used to turn text into vectors: for example, embedding conversation turns before storing them in a vector database, and embedding a query at retrieval time.

#### Request

```text
POST /embed
Content-Type: application/json
X-API-Key: your_api_key_here
```

**Single string:**
```json
{ "input": "the quick brown fox" }
```

**Batch (preferred when embedding several items at once):**
```json
{ "input": ["first chunk", "second chunk", "third chunk"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input` | string or array of strings | Yes | Text to embed. A list is embedded in a single provider call (batch). Cannot be empty. |

The caller sends no `model` field — the embedding route is selected by URL (`EMBEDDING_MODEL_URL`), the same way `/chat` selects an endpoint by URL rather than a model name in the body. There is no `reasoning_effort` — the embedding model does not reason. (The proxy itself may add a `model` to the *provider* payload when the server-side `EMBEDDING_MODEL_NAME` is configured — some embedding runtimes require it — but that is `openagent-infra` configuration, not a caller field. See the Environment Variables Reference.)

#### Response

```text
HTTP/1.1 200 OK
Content-Type: application/json
```

The provider's OpenAI-compatible embeddings JSON, passed through unchanged:

```json
{
  "object": "list",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, "..."] }
  ],
  "model": "<provider model id>",
  "usage": { "prompt_tokens": 7, "total_tokens": 7 }
}
```

For a batch input, `data` contains one entry per input string, each tagged with its `index`. The vector dimensionality is whatever the embedding model emits — `openagent-infra` does not pin or transform it.

#### Error responses

| Status | Condition | Body |
|---|---|---|
| `400` | `input` is empty (empty string or empty list) | `{"detail": "Input cannot be empty"}` |
| `401` | X-API-Key header missing or invalid | `{"detail": "Invalid or missing API key"}` |
| `422` | Request body malformed (e.g. `input` missing or wrong type) | FastAPI validation error JSON |
| `502` | Embedding provider returned a non-200, or an unexpected proxy error | `{"detail": "Embedding provider returned <status>"}` |
| `503` | `EMBEDDING_MODEL_URL` not set, or the embedding provider host is not reachable | `{"detail": "Embedding model not configured"}` / `{"detail": "Embedding provider is not reachable"}` |

Unlike `/chat`, `/embed` is a single JSON response, so provider failures are returned as real HTTP error status codes rather than in-stream events.

#### Example — curl (testing only)

```bash
curl -X POST http://localhost:8002/embed \
     -H "Content-Type: application/json" \
     -H "X-API-Key: your_api_key_here" \
     -d '{"input": ["first chunk", "second chunk"]}'
```

---

### `GET /health`

Lightweight health check. No authentication required. Probes the proxy and all three provider endpoints independently and concurrently.

#### Request

```text
GET /health
```

#### Response — fully ready

```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "ok", "embedding": "ok"}
```

#### Response — base provider host unreachable

```json
{"status": "degraded", "proxy": "ok", "base_model": "unreachable", "nervous_system": "ok", "embedding": "ok"}
```

#### Response — nervous-system / embedding not yet configured

```json
{"status": "ok", "proxy": "ok", "base_model": "ok", "nervous_system": "not configured", "embedding": "not configured"}
```

**Note:** Always returns HTTP `200`. `status` is `ok` when the base provider **host** is reachable, and `degraded` only when it is not. `/health` answers *"is the provider host reachable?"*, not *"is the model warm?"*: a reachable host whose worker is cold (scale-to-zero, still spinning up) reports `ok` — the cold start is absorbed at `/chat` / `/embed` time, where the read timeout is unbounded. `nervous_system` and `embedding` are checked independently and do **not** affect the top-level `status` (either may be unconfigured). `not configured` means the corresponding URL is unset in `.env`.

Because a cold-but-reachable worker reports `ok`, `/health` is **not** a "model is warm" signal. A caller can poll it to confirm the base host is reachable, but warmth is only discovered at call time — the first `/chat` or `/embed` after an idle period absorbs the cold start. The top-level `status` is the field a caller reads for host reachability; the per-endpoint fields are diagnostic detail.

```bash
curl http://localhost:8002/health
```

---

### `GET /docs`

Auto-generated Swagger UI. For interactive testing only.

```text
http://localhost:8002/docs
```

---

## System Prompt

The system prompt — the persona — is owned by **openagent-api**, not `openagent-infra`. `openagent-api` loads it once at startup and sends it as the first `system` message in the messages list on every `/chat` request.

`openagent-infra` automatically appends `Reasoning: <level>` to that system message before forwarding to the provider — the caller does not include the reasoning instruction manually.

`openagent-infra` has no knowledge of what the system prompt contains. It receives the full messages list, injects the reasoning level, and forwards. The persona text passes through untouched. The `/embed` route carries no persona — it forwards raw input strings only.

**openagent-api is responsible for:**
- Owning and managing the persona (`src/prompt/bio.txt`, baked into its image).
- Including it as `{"role": "system", "content": "..."}` as the first message on every `/chat` request.
- Setting `reasoning_effort` per request based on the use case (or omitting it to let `openagent-infra` default).

---

## SSE Stream Specification

(Applies to `/chat` only. `/embed` returns a single JSON body, not a stream.)

### Event format

```text
data: <JSON ChatCompletion chunk>\n\n
```

Each event payload is a JSON-encoded OpenAI ChatCompletion chunk. The double newline `\n\n` terminates each event.

### Stream lifecycle

```text
[connection established]
     │
     ▼
data: {... "delta": {"reasoning": "..."} ...}\n\n   ← reasoning chain begins
     │
     ▼
data: {... "delta": {"reasoning": "..."} ...}\n\n   ← reasoning continues
     │
     ▼
data: {... "delta": {"content": "..."} ...}\n\n     ← final answer tokens begin
     │
     ▼
data: {... "delta": {}, "finish_reason": "stop" ...}\n\n   ← terminal chunk
     │
     ▼
data: [DONE]\n\n                                    ← stream complete
     │
     ▼
[connection closed]
```

### Handling the reasoning chain

Both the base model and the control-layer model are reasoning models. Every response from either model includes a reasoning chain (in `delta.reasoning`) before the final answer (in `delta.content`). (The embedding model is not a reasoning model and is not reached via this stream.)

Three options for handling on the caller side:

**Option 1 — Hide entirely:** Filter `delta.reasoning` tokens before displaying. Show only `delta.content`.

**Option 2 — Show in a collapsible:** Display reasoning in a collapsed "Show thinking" section, answer in the main surface. (This is what `openagent-frontend` does.)

**Option 3 — Show everything:** Stream all tokens directly to the UI as-is.

The choice is entirely a caller/frontend decision. `openagent-infra` streams everything byte-for-byte and takes no position on display.

---

## Technical Specifications

### Models

`openagent-infra` routes between three logical models, each reached over an OpenAI-compatible API. The actual weights, quantization, parameter counts, context window, and serving runtime are entirely the BYOC provider's concern and are not pinned by `openagent-infra`.

**base model (primary)**

| Property | Value |
|---|---|
| Selector | `model="base"` on `/chat` (default; also the value when `model` is omitted) |
| Endpoint | `BASE_MODEL_URL` (proxy appends `/v1/chat/completions`) |
| Model name | `BASE_MODEL_NAME` (optional config) — when set, the proxy adds `"model": <name>` to the forwarded payload; empty by default (the current base worker needs none). |
| Type | Reasoning model — emits a reasoning chain before the answer |
| API | OpenAI-compatible (`/chat/completions`, SSE streaming) |
| Role | Default model — all everyday OpenAgent conversations |

**nervous-system model (control layer)**

| Property | Value |
|---|---|
| Selector | `model="nervous_system"` on `/chat` |
| Endpoint | `NERVOUS_SYSTEM_URL` (optional — when unset, the route is "not configured"; proxy appends `/v1/chat/completions`) |
| Model name | `NERVOUS_SYSTEM_MODEL_NAME` (optional config) — when set, the proxy adds `"model": <name>` to the forwarded payload; empty by default. |
| Type | Reasoning model — emits a reasoning chain before the answer |
| API | OpenAI-compatible (`/chat/completions`, SSE streaming) |
| Role | Fast control layer — routing, history filtering, agent decisions |

**embedding model**

| Property | Value |
|---|---|
| Selector | `POST /embed` (caller sends no `model` field — route selected by URL) |
| Endpoint | `EMBEDDING_MODEL_URL` (optional — when unset, `/embed` returns "not configured"; proxy appends `/v1/embeddings`) |
| Model name | `EMBEDDING_MODEL_NAME` (optional config) — when set, the proxy adds `"model": <name>` to the forwarded payload. Required by some runtimes (e.g. BGE-M3 returns 500 without it; set `BAAI/bge-m3`). |
| Type | Embedding model — turns text into vectors. Not a reasoning model; no reasoning chain. |
| API | OpenAI-compatible (`/v1/embeddings`, single JSON response) |
| Role | Text → vector for retrieval (e.g. conversation-history search) |

Because the models are reached purely as OpenAI-compatible endpoints, you can point the URLs at the same provider or different providers, at different model sizes, balancing compute cost against latency as you see fit. `openagent-infra` does not care.

> **Embedding note.** Through the OpenAI-compatible `/v1/embeddings` route, `openagent-infra` forwards and returns the **dense** embedding vector. If you serve a model with additional representations (e.g. BGE-M3's sparse / multi-vector outputs), those are not exposed by the standard embeddings endpoint and would require a different serving path — out of scope for this proxy's pass-through contract.

### Generation parameters

| Parameter | Value | Notes |
|---|---|---|
| `reasoning_effort` | `low` / `medium` / `high` | First-class `/chat` field — controls reasoning depth and latency. Injected into the system message as `Reasoning: <level>`. Applies to the two chat models only. |
| Batching | Provider-dependent | Continuous batching, concurrency, and queueing are the provider's concern. For `/embed`, pass a list to batch multiple inputs in one call. |

### Infrastructure

| Property | Value |
|---|---|
| Inference provider | BYOC — any OpenAI-compatible endpoint(s) (e.g. RunPod, OpenAI, a local vLLM/Ollama runtime) |
| Base model endpoint | `BASE_MODEL_URL` (base URL; proxy appends `/v1/chat/completions`) |
| Control-layer endpoint | `NERVOUS_SYSTEM_URL` (optional; base URL) |
| Embedding endpoint | `EMBEDDING_MODEL_URL` (optional; base URL; proxy appends `/v1/embeddings`) |
| Endpoint format | OpenAI-compatible chat-completions / embeddings API |
| Scaling | Provider-dependent; serverless endpoints scale to zero independently when idle |

`openagent-infra` itself runs no model and holds no weights — it is a thin async proxy. All GPU/compute concerns live with the provider.

### Container

| Property | Value |
|---|---|
| Base image | `python:3.12-slim` |
| WORKDIR | `/app` |
| Proxy port | `8002` (public) |
| Env file | `.env` at project root |
| GPU required | No — inference runs at the BYOC provider |
| Volume required | No — no weights, no state |

### Startup timing

| Phase | Approximate duration |
|---|---|
| Proxy startup | < 10 seconds (no model to load) |
| Provider cold start (serverless) | Provider-dependent; can be minutes for a worker spinning up from zero |
| Provider warm | Provider-dependent; typically seconds |

If your BYOC endpoints are serverless and scale to zero, the first request after an idle period waits for the provider's worker to spin up. Each endpoint has an independent cold-start cycle. Design the caller with a loading state for this case — the cold start is absorbed at `/chat` / `/embed` time. Note that `/health` does **not** report `degraded` for a cold worker: a reachable host reports `ok` even while its model is warming (see `GET /health`).

### Generation timing

| Scenario | Reasoning | Approximate duration |
|---|---|---|
| Simple greeting | low | 5–15 seconds |
| Short factual question | medium | 15–45 seconds |
| Complex reasoning task | high | 1–3 minutes |
| Embedding (warm) | n/a | Milliseconds to a few seconds |
| Embedding (cold worker) | n/a | Cold-start wait, then milliseconds |

Actual numbers depend entirely on the provider, the model, and load. Treat these as order-of-magnitude expectations, not guarantees.

---

## Integration Notes for Callers

`openagent-api` is the primary caller of `openagent-infra` today; other server-side services may call it as the system grows. The integration points:

### Readiness check pattern

Poll `/health` until `status` is `"ok"` to confirm the base provider **host** is reachable. `status` is `degraded` only when the base host cannot be reached — *not* merely because a worker is cold. A cold/scale-to-zero worker on a reachable host already reports `ok`; its cold start surfaces as latency on the first `/chat` or `/embed`, not as `degraded`.

```text
GET /health  →  {"status": "degraded", ...}   # base provider host unreachable (down / wrong URL)
GET /health  →  {"status": "ok", ...}          # base host reachable (worker may still be cold)
```

### Selecting the model per request (`/chat`)

Pass `model` in the request body to route to a specific chat endpoint. If omitted, every request routes to the base model — the pipeline is unbroken for callers that never set it. `openagent-api` omits the field on every call today (it always uses the base model); the `nervous_system` route is available for callers that need a fast control model.

```text
{ "messages": [...], "reasoning_effort": "medium", "model": "base" }            # default
{ "messages": [...], "reasoning_effort": "low",    "model": "nervous_system" }  # control layer
```

### Embedding requests (`/embed`)

POST a string or a list of strings to `/embed` with the same `X-API-Key`. The response is the provider's OpenAI-compatible embeddings JSON; read `data[i].embedding` for each input. Batch by passing a list. `openagent-infra` only turns text into vectors — storing and searching those vectors (the vector database and retrieval logic) is the caller's concern. If `EMBEDDING_MODEL_URL` is unset, `/embed` returns `503` and the `/chat` path is entirely unaffected.

### Setting reasoning effort per request (`/chat`)

Pass `reasoning_effort` in the request body. If omitted, the server default (`medium`, from the `REASONING_EFFORT` env var) applies. Applies to both chat models; the embedding model does not reason.

### Constructing the messages list (`/chat`)

The caller builds the full messages list, including the persona as the first `system` message. `openagent-infra` injects the reasoning level — the caller does not add it.

### Token handling (`/chat`)

Each SSE event is a JSON ChatCompletion chunk. The caller's decoder must `json.loads()` each `data:` payload and route `choices[0].delta.reasoning` and `choices[0].delta.content` to the appropriate surfaces. The stream terminates with an empty-delta `finish_reason: "stop"` chunk followed by `data: [DONE]`. Watch the stream for a `data: [ERROR] ...` payload too — that is how provider-side failures surface on `/chat`.

### API key handling

`API_KEY` is the caller↔infra secret (the same value `openagent-api` holds as `INFRA_API_KEY`). `PROVIDER_API_KEY` never leaves `openagent-infra` — callers do not need it and never see it. Multiple server-side callers share `API_KEY` today; per-caller keys are a later evolution.

### Cold start handling

Serverless provider endpoints scale to zero when idle. The first request after inactivity triggers a cold start, absorbed at `/chat` / `/embed` time (unbounded read timeout). `/health` does **not** go `degraded` for a cold worker — it reports `degraded` only when the host itself is unreachable.

### Long generation times

Even at `low` effort, generation takes several seconds; a cold worker can take minutes. The caller should not set a short read timeout — `openagent-api` uses an unbounded read timeout on this boundary for exactly this reason. The same applies to `/embed` against a serverless embedding endpoint.

### CORS

`openagent-infra` does not configure CORS headers. It is called server-side, never directly from a browser. This keeps `PROVIDER_API_KEY` server-side and out of any client.

### The `[DONE]` sentinel (`/chat`)

Always handle `[DONE]` explicitly — it signals generation is complete and the connection can be closed.

### 401 handling

A `401` indicates a key configuration error between the caller and `openagent-infra` (`INFRA_API_KEY` on the api side does not match `API_KEY` on the infra side). Log it for the operator — it is not an end-user-facing error.

---

## Environment Variables Reference

| Variable | Type | Default | Description |
|---|---|---|---|
| `API_KEY` | string | — | Secret validated against the `X-API-Key` header on `/chat` and `/embed`. Must match `openagent-api`'s `INFRA_API_KEY` byte-for-byte. Required. |
| `BASE_MODEL_URL` | string | — | OpenAI-compatible **base** endpoint for the base model — provider root, no path; the proxy appends `/v1/chat/completions`. Default route for all `/chat` requests. Required. |
| `NERVOUS_SYSTEM_URL` | string | — | OpenAI-compatible **base** endpoint for the control-layer model. Same base form; proxy appends `/v1/chat/completions`. Used when `model="nervous_system"`. Optional — when unset, that route reports "not configured". |
| `EMBEDDING_MODEL_URL` | string | — | OpenAI-compatible **base** endpoint for the embedding model. Same base form; proxy appends `/v1/embeddings`. Used by `POST /embed`. Optional — when unset, `/embed` returns "not configured" and `/chat` is unaffected. |
| `PROVIDER_API_KEY` | string | — | Bearer credential for the BYOC provider endpoint(s). Never exposed to callers. Required. |
| `REASONING_EFFORT` | string | `medium` | Server default reasoning level for the chat models. Overridable per `/chat` request. |
| `BASE_MODEL_NAME` | string | `""` | Optional. When set, the proxy adds `"model": <name>` to the forwarded payload for `model="base"` `/chat` requests; when empty, no model field is sent. Set only if the base endpoint requires an explicit model field. |
| `NERVOUS_SYSTEM_MODEL_NAME` | string | `""` | Optional. Same as `BASE_MODEL_NAME`, for `model="nervous_system"` `/chat` requests. |
| `EMBEDDING_MODEL_NAME` | string | `""` | Optional. When set, the proxy adds `"model": <name>` to the `/embed` payload; when empty, no model field is sent. Required by embedding runtimes that demand a model field — e.g. BGE-M3 returns `500` without it (set `BAAI/bge-m3`). |

---

## Known Behaviors

| Behavior | Cause | Caller handling |
|---|---|---|
| Reasoning tokens before the final answer (`/chat`) | Both chat models are reasoning models — they reason before answering | Filter or display per UX choice (reasoning is in `delta.reasoning`, answer in `delta.content`) |
| `low` effort is faster but shallower | Less reasoning chain generated | Use for lightweight calls only |
| `high` effort significantly slower | Extended reasoning chain | Show a loading indicator; no short read timeout |
| `degraded` on `/health` | Base provider **host** is unreachable (down / wrong URL / connection refused) — NOT merely a cold worker | Check the provider's console for endpoint/worker status |
| Cold worker still reports `ok` on `/health` | A reachable host whose model is warming reports reachable, by design | Don't treat `/health` as a "warm" signal; the cold start is absorbed on the first `/chat` / `/embed` |
| `[ERROR]` event inside a `/chat` 200 stream | Provider unreachable or returned a non-200 after the stream began | Watch the SSE stream for `data: [ERROR] ...`, not just the HTTP status |
| `401` on a valid-looking request | `API_KEY` (infra) and `INFRA_API_KEY` (api) mismatch | Configuration error — align the two values |
| `422` on a malformed request | Pydantic validation failed | `/chat`: send a `messages` array with valid `role`/`content`. `/embed`: send `input` as a string or list of strings |
| `400` missing user message (`/chat`) | No `user`-role message in `messages` | Always include at least one user message |
| `400` empty input (`/embed`) | `input` is an empty string or empty list | Send non-empty text |
| `503` on `/embed` — "not configured" | `EMBEDDING_MODEL_URL` not set in `.env` | Set the embedding endpoint URL once the model is deployed |
| `503` on `/embed` — "not reachable" | Embedding provider host unreachable | Check the provider's console for endpoint/worker status |
| `502` on `/embed` | Embedding provider returned a non-200, or a proxy-level error. A common cause is a runtime that requires a `model` field and returns `500` without it (BGE-M3 does). | Inspect the operator logs for the upstream status. If the upstream is `500`, set `EMBEDDING_MODEL_NAME` (e.g. `BAAI/bge-m3`) so the proxy includes a model name. |
| `nervous_system` routes to the control model | `model="nervous_system"` field set | Confirm `NERVOUS_SYSTEM_URL` is set in `.env` |
| `nervous_system` / `embedding` shows "not configured" on `/health` | The corresponding URL not set in `.env` | Add the endpoint URL once that model is deployed |
| Reasoning-format delimiters occasionally in the `/chat` stream | Some provider serving runtimes don't fully parse the model's reasoning format | `openagent-infra` forwards bytes unchanged; the caller's display policy handles it. Resolves when the provider's runtime supports the model's reasoning parser. |

---

## Design Decisions

### Why keep a FastAPI proxy instead of calling providers directly?

A provider usually offers a single shared API key. Keeping FastAPI as a proxy puts caller authentication in one place and keeps the provider's billing credential out of the gateway and frontend layers. It also gives a stable, OpenAI-shaped contract regardless of which provider sits behind it. It is also the single chokepoint every model path runs through — chat and embeddings alike — so no other service ever holds `PROVIDER_API_KEY`.

### Why a separate `/embed` endpoint instead of a `model` route on `/chat`?

`/chat` is welded to the messages-in / SSE-stream-out contract. Embeddings have a different request shape (raw input strings), a different response (a single JSON vector array, no streaming), a different upstream path (`/v1/embeddings`), and no reasoning. Forcing that through `/chat` would break the contract both ways, so embeddings get their own endpoint while reusing the same auth, the same `PROVIDER_API_KEY`, and the same base-URL convention.

### Why does `/embed` return real error codes when `/chat` cannot?

`/chat` commits an `HTTP 200` the moment the SSE stream begins, so a later provider failure can only be reported as an in-stream `[ERROR]` event. `/embed` is a single, non-streaming response, so it can and does return real HTTP status codes (`400` / `502` / `503`).

### Why two separate API keys?

`API_KEY` authenticates the caller to `openagent-infra`. `PROVIDER_API_KEY` authenticates `openagent-infra` to the inference backend. Separating them means the caller key can be rotated without touching the provider configuration, and the provider key never leaves this service. The caller-side validation is isolated in `verify_api_key` so it can later move from a single shared key to per-caller keys without changing the endpoint contract.

### Why reasoning effort as an API field?

Both chat models support configurable reasoning depth. Exposing it per request lets a frontend offer Quick / Standard / Deep modes and lets tooling set depth per use case. The proxy injects it into the system message so callers never hand-write the instruction.

### Why the OpenAI messages / embeddings format?

It makes `openagent-infra` compatible with almost any frontend and model backend, keeps the serving layer stateless, and keeps the wire protocol standard and boring — which is what you want in an inference proxy.

### Why byte-for-byte SSE relay (and JSON passthrough)?

Anything the proxy parses on the relay path, it can break. Forwarding the provider's chat stream unchanged, and passing the embeddings JSON through unchanged, keeps the proxy simple and lets the caller's decoder be the single place that understands the payload format.

### Why port 8002?

Port convention: 8000 = openagent-frontend, 8001 = openagent-api, 8002 = openagent-infra, 8003 = openagent-logger. The numbering reflects the request flow.

---

*openagent-infra — part of the OpenAgent system*