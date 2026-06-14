"""
tests/test_main.py

Tests for the openagent-infra proxy (src/api/main.py).

Scope: authentication and the pure request-shaping/validation logic — no real
provider is contacted. All the paths exercised here reject the request BEFORE
any upstream HTTP call, or are pure functions, so the tests are hermetic.

Env config is set up in conftest.py before the module is imported.
"""

import pytest
from fastapi import HTTPException

from src.api.main import Message, inject_reasoning_effort, verify_api_key

from .conftest import TEST_API_KEY


# ---------------------------------------------------------------------------
# inject_reasoning_effort — pure request-shaping logic
# ---------------------------------------------------------------------------
class TestInjectReasoningEffort:
    def test_appends_reasoning_to_single_system_message(self):
        messages = [
            Message(role="system", content="You are OpenAgent."),
            Message(role="user", content="hello"),
        ]
        result = inject_reasoning_effort(messages, "high")

        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are OpenAgent.\nReasoning: high"

    def test_targets_the_last_system_message_when_several_exist(self):
        messages = [
            Message(role="system", content="first system"),
            Message(role="user", content="hi"),
            Message(role="system", content="second system"),
        ]
        result = inject_reasoning_effort(messages, "low")

        # The earlier system message is untouched.
        assert result[0]["content"] == "first system"
        # Only the LAST system message gets the reasoning instruction.
        assert result[2]["content"] == "second system\nReasoning: low"

    def test_user_and_assistant_messages_are_untouched(self):
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
        ]
        result = inject_reasoning_effort(messages, "medium")

        assert result[1] == {"role": "user", "content": "question"}
        assert result[2] == {"role": "assistant", "content": "answer"}

    def test_creates_system_message_when_none_present(self):
        messages = [Message(role="user", content="just a user turn")]
        result = inject_reasoning_effort(messages, "medium")

        assert result[0] == {"role": "system", "content": "Reasoning: medium"}
        assert result[1] == {"role": "user", "content": "just a user turn"}

    def test_does_not_mutate_input_messages(self):
        # The contract: it builds fresh dicts from the Message objects, so the
        # original Message instances must be unchanged after the call.
        original_content = "You are OpenAgent."
        messages = [
            Message(role="system", content=original_content),
            Message(role="user", content="hello"),
        ]
        inject_reasoning_effort(messages, "high")

        assert messages[0].content == original_content

    def test_returns_plain_dicts(self):
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="u"),
        ]
        result = inject_reasoning_effort(messages, "low")

        assert all(isinstance(m, dict) for m in result)
        assert all(set(m.keys()) == {"role", "content"} for m in result)


# ---------------------------------------------------------------------------
# verify_api_key — dependency-level auth logic (called directly with await)
# ---------------------------------------------------------------------------
class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_correct_key_passes(self):
        result = await verify_api_key(key=TEST_API_KEY)
        assert result == TEST_API_KEY

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key="totally-wrong-key")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key="")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_none_key_raises_401(self):
        # APIKeyHeader with auto_error=False passes None when the header is absent.
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key=None)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# resolve_chat_base_url — pure model -> URL routing
# ---------------------------------------------------------------------------
class TestResolveChatBaseUrl:
    def test_base_routes_to_base_url(self, main):
        assert main.resolve_chat_base_url("base") == main.BASE_MODEL_URL

    def test_nervous_system_routes_to_nervous_url(self, main):
        assert (
            main.resolve_chat_base_url("nervous_system")
            == main.NERVOUS_SYSTEM_URL
        )

    def test_unexpected_model_raises_value_error(self, main):
        with pytest.raises(ValueError):
            main.resolve_chat_base_url("not_a_real_model")

    def test_unconfigured_nervous_system_returns_falsy(self, main, monkeypatch):
        # When NERVOUS_SYSTEM_URL is empty the resolver returns the empty string,
        # which the /chat endpoint treats as "not configured" (503).
        monkeypatch.setattr(main, "NERVOUS_SYSTEM_URL", "")
        assert main.resolve_chat_base_url("nervous_system") == ""


# ---------------------------------------------------------------------------
# /chat endpoint — auth + pre-stream validation via TestClient
#
# All cases here are rejected BEFORE any provider call: bad/missing auth,
# empty messages, missing user message, and an unconfigured route. None of
# them open the StreamingResponse, so no upstream is contacted.
# ---------------------------------------------------------------------------
class TestChatEndpoint:
    def _body(self, **overrides):
        body = {
            "messages": [
                {"role": "system", "content": "You are OpenAgent."},
                {"role": "user", "content": "hello"},
            ],
            "reasoning_effort": "medium",
        }
        body.update(overrides)
        return body

    def test_no_api_key_returns_401(self, client):
        resp = client.post("/chat", json=self._body())
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, client):
        resp = client.post(
            "/chat",
            json=self._body(),
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_valid_key_empty_messages_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/chat",
            json={"messages": [], "reasoning_effort": "medium"},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_valid_key_no_user_message_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/chat",
            json={
                "messages": [{"role": "system", "content": "only a system msg"}],
                "reasoning_effort": "medium",
            },
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400
        assert "user" in resp.json()["detail"].lower()

    def test_valid_key_unconfigured_nervous_system_returns_503(
        self, client, valid_api_key, main, monkeypatch
    ):
        # Force the nervous-system route to be unconfigured and confirm the
        # endpoint returns a real 503 before any streaming begins.
        monkeypatch.setattr(main, "NERVOUS_SYSTEM_URL", "")
        resp = client.post(
            "/chat",
            json=self._body(model="nervous_system"),
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /embed endpoint — auth + validation (no provider configured in test env)
# ---------------------------------------------------------------------------
class TestEmbedEndpoint:
    def test_no_api_key_returns_401(self, client):
        resp = client.post("/embed", json={"input": "hello"})
        assert resp.status_code == 401

    def test_valid_key_empty_string_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/embed",
            json={"input": "   "},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400

    def test_valid_key_empty_list_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/embed",
            json={"input": []},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400

    def test_unconfigured_embedding_url_returns_503(self, client, valid_api_key):
        # EMBEDDING_MODEL_URL is unset in the test env (see conftest), so a
        # valid, non-empty request reaches the "not configured" guard before
        # any provider call.
        resp = client.post(
            "/embed",
            json={"input": "valid text"},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /health endpoint — no auth required, must return 200 with the right shape
#
# The probes will fail (no real provider), so status is "degraded" / providers
# "unreachable" — we assert HTTP 200 and the JSON shape, not reachability.
# ---------------------------------------------------------------------------
class TestHealthEndpoint:
    def test_health_returns_200_without_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_json_shape(self, client):
        body = client.get("/health").json()
        for field in ("status", "proxy", "base_model", "nervous_system", "embedding"):
            assert field in body
        assert body["proxy"] == "ok"
        assert body["status"] in ("ok", "degraded")
        # EMBEDDING_MODEL_URL is unset in the test env -> "not configured".
        assert body["embedding"] == "not configured"
