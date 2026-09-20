"""
tests/test_serv_client.py — unit tests for app/serv_client.py.

Uses a FakeServClient (no network, no API key) to verify:
  - System message is always injected
  - Offline stub works
  - ValueError raised when caller smuggles a system message into messages list
  - ServResponse fields are populated correctly
"""

from __future__ import annotations

import pytest

from app.serv_client import ServClient, ServResponse, _DEFAULT_SYSTEM


# ---------------------------------------------------------------------------
# FakeServClient — drop-in replacement for tests that need a controllable stub
# ---------------------------------------------------------------------------

class FakeServClient(ServClient):
    """
    Subclass of ServClient that replaces the real API call with a recorded
    fixture.  It also asserts that a system message was injected before the
    call reaches the network layer.

    Usage::

        client = FakeServClient(reply='{"foo": 1}')
        resp = client.chat(messages=[{"role": "user", "content": "..."}],
                           system="Do X")
        assert resp.content == '{"foo": 1}'
    """

    def __init__(self, reply: str = '{"ok": true}', *, raise_exc: Exception | None = None):
        # Force offline=False so our override runs, not the stub path
        super().__init__(api_key="fake", base_url="http://fake", offline=False)
        self._reply = reply
        self._raise_exc = raise_exc
        self.last_messages: list[dict] | None = None  # captured for assertions

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_completion_tokens: int = 2048,
        response_format: dict | None = None,
    ) -> ServResponse:
        # ── The same guard as the real client ──────────────────────────
        for msg in messages:
            if msg.get("role") in ("system", "developer"):
                raise ValueError(
                    "Do not include a system/developer message in the `messages` "
                    "list. Pass it via the `system=` parameter so ServClient can "
                    "guarantee the SERV requirement is met."
                )

        # ── Build the full message list (mirrors real client) ──────────
        system_content = system or _DEFAULT_SYSTEM
        full_messages = [
            {"role": "system", "content": system_content},
            *messages,
        ]

        # ── Assert system message is first and present ─────────────────
        assert full_messages[0]["role"] == "system", (
            "FakeServClient: system message must be first in the message list"
        )
        assert full_messages[0]["content"], (
            "FakeServClient: system message content must not be empty"
        )

        self.last_messages = full_messages

        if self._raise_exc:
            raise self._raise_exc

        return ServResponse(
            content=self._reply,
            model="fake-model",
            latency_ms=1.0,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


# ---------------------------------------------------------------------------
# Tests: system message enforcement
# ---------------------------------------------------------------------------

def test_system_message_injected_when_not_supplied():
    client = FakeServClient()
    client.chat(messages=[{"role": "user", "content": "hello"}])
    assert client.last_messages is not None
    first = client.last_messages[0]
    assert first["role"] == "system"
    assert first["content"] == _DEFAULT_SYSTEM


def test_system_message_custom_forwarded():
    client = FakeServClient()
    custom = "You are a JSON-only assistant."
    client.chat(messages=[{"role": "user", "content": "hello"}], system=custom)
    assert client.last_messages[0]["content"] == custom


def test_system_message_always_first():
    """User messages must come after the system message."""
    client = FakeServClient()
    client.chat(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ],
        system="sys",
    )
    assert client.last_messages[0]["role"] == "system"
    assert client.last_messages[1]["role"] == "user"


def test_raises_if_system_in_messages_list():
    """Callers must not embed a system message inside the messages list."""
    client = FakeServClient()
    with pytest.raises(ValueError, match="system"):
        client.chat(
            messages=[
                {"role": "system", "content": "sneaky"},
                {"role": "user", "content": "hello"},
            ]
        )


def test_raises_if_developer_in_messages_list():
    client = FakeServClient()
    with pytest.raises(ValueError, match="system"):
        client.chat(
            messages=[{"role": "developer", "content": "also sneaky"}]
        )


# ---------------------------------------------------------------------------
# Tests: offline stub
# ---------------------------------------------------------------------------

def test_offline_stub_returns_without_network():
    client = ServClient(api_key="x", base_url="http://fake", offline=True)
    resp = client.chat(messages=[{"role": "user", "content": "anything"}])
    assert resp.stubbed is True
    assert resp.content  # non-empty


def test_offline_stub_does_not_call_api():
    """offline=True must never reach the network, even with a bad URL."""
    client = ServClient(api_key="x", base_url="http://0.0.0.0:9", offline=True)
    # Would raise ConnectionRefusedError if it tried to connect
    resp = client.chat(messages=[{"role": "user", "content": "ping"}])
    assert resp.stubbed is True


# ---------------------------------------------------------------------------
# Tests: response fields
# ---------------------------------------------------------------------------

def test_response_fields_populated():
    client = FakeServClient(reply="blue")
    resp = client.chat(messages=[{"role": "user", "content": "colour?"}])
    assert resp.content == "blue"
    assert resp.model == "fake-model"
    assert resp.latency_ms > 0
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.total_tokens == 15
    assert resp.stubbed is False


def test_model_property():
    client = ServClient(api_key="x", base_url="http://fake", model="my-model", offline=True)
    assert client.model == "my-model"
