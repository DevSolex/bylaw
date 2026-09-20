"""
tests/test_serv_client.py — unit tests for app/serv_client.py.

Exports FakeServClient for use in other test modules.
"""

from __future__ import annotations

import json
import pytest

from app.serv_client import ServClient, ServResponse, _DEFAULT_SYSTEM


# ---------------------------------------------------------------------------
# FakeServClient
# ---------------------------------------------------------------------------

class FakeServClient(ServClient):
    """
    Drop-in replacement for ServClient used in offline tests.

    Pass reply= for a fixed reply, or replies= for a sequence (one per call).
    Asserts system message is always present and first.
    """

    def __init__(
        self,
        reply: str = '{"ok": true}',
        *,
        replies: list[str] | None = None,
        raise_exc: Exception | None = None,
    ):
        super().__init__(api_key="fake", base_url="http://fake", offline=False)
        self._reply = reply
        self._replies = list(replies) if replies else None
        self._raise_exc = raise_exc
        self._call_index = 0
        self.last_messages: list[dict] | None = None
        self.all_calls: list[list[dict]] = []

    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        max_completion_tokens: int = 2048,
        response_format: dict | None = None,
        _stub_vaults=None,
    ) -> ServResponse:
        for msg in messages:
            if msg.get("role") in ("system", "developer"):
                raise ValueError(
                    "Do not include a system/developer message in the `messages` "
                    "list. Pass it via the `system=` parameter."
                )

        system_content = system or _DEFAULT_SYSTEM
        full_messages = [{"role": "system", "content": system_content}, *messages]
        assert full_messages[0]["role"] == "system"
        assert full_messages[0]["content"]

        self.last_messages = full_messages
        self.all_calls.append(full_messages)

        if self._raise_exc:
            raise self._raise_exc

        if self._replies is not None and self._call_index < len(self._replies):
            content = self._replies[self._call_index]
        else:
            content = self._reply
        self._call_index += 1

        return ServResponse(
            content=content,
            model="fake-model",
            latency_ms=1.0,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


# ---------------------------------------------------------------------------
# System message enforcement
# ---------------------------------------------------------------------------

def test_system_message_injected_when_not_supplied():
    client = FakeServClient()
    client.chat(messages=[{"role": "user", "content": "hello"}])
    assert client.last_messages[0]["role"] == "system"
    assert client.last_messages[0]["content"] == _DEFAULT_SYSTEM


def test_system_message_custom_forwarded():
    client = FakeServClient()
    client.chat(messages=[{"role": "user", "content": "hello"}], system="custom sys")
    assert client.last_messages[0]["content"] == "custom sys"


def test_system_message_always_first():
    client = FakeServClient()
    client.chat(
        messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        system="sys",
    )
    assert client.last_messages[0]["role"] == "system"
    assert client.last_messages[1]["role"] == "user"


def test_raises_if_system_in_messages_list():
    client = FakeServClient()
    with pytest.raises(ValueError, match="system"):
        client.chat(messages=[{"role": "system", "content": "sneaky"}])


def test_raises_if_developer_in_messages_list():
    client = FakeServClient()
    with pytest.raises(ValueError, match="system"):
        client.chat(messages=[{"role": "developer", "content": "sneaky"}])


# ---------------------------------------------------------------------------
# Offline stub
# ---------------------------------------------------------------------------

def test_offline_stub_returns_without_network():
    client = ServClient(api_key="x", base_url="http://fake", offline=True)
    resp = client.chat(messages=[{"role": "user", "content": "anything"}])
    assert resp.stubbed is True
    assert resp.content


def test_offline_stub_does_not_call_api():
    client = ServClient(api_key="x", base_url="http://0.0.0.0:9", offline=True)
    resp = client.chat(messages=[{"role": "user", "content": "ping"}])
    assert resp.stubbed is True


def test_offline_stub_dynamic_vaults():
    """Stub must use actual vault IDs, not hardcoded names."""
    from datetime import datetime, timezone
    from app.models import Vault

    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    vaults = [
        Vault(id="v-alpha", name="Alpha", chain="eth", asset="USDC", risk=2, fetched_at=now),
        Vault(id="v-beta",  name="Beta",  chain="eth", asset="USDC", risk=3, fetched_at=now),
    ]
    client = ServClient(api_key="x", base_url="http://fake", offline=True)
    resp = client.chat(
        messages=[{"role": "user", "content": "propose"}],
        _stub_vaults=vaults,
    )
    assert resp.stubbed is True
    data = json.loads(resp.content)
    alloc = data["allocation"]
    assert "v-alpha" in alloc
    assert "v-beta" in alloc
    assert "mock-tbill" not in alloc
    assert abs(sum(alloc.values()) - 1.0) < 1e-4


def test_offline_stub_has_token_counts():
    """Stub must return non-zero token counts for accounting."""
    client = ServClient(api_key="x", base_url="http://fake", offline=True)
    resp = client.chat(messages=[{"role": "user", "content": "x"}])
    assert (resp.prompt_tokens or 0) > 0
    assert (resp.completion_tokens or 0) > 0
    assert (resp.total_tokens or 0) > 0


# ---------------------------------------------------------------------------
# Response fields / sequence
# ---------------------------------------------------------------------------

def test_response_fields_populated():
    client = FakeServClient(reply="blue")
    resp = client.chat(messages=[{"role": "user", "content": "colour?"}])
    assert resp.content == "blue"
    assert resp.model == "fake-model"
    assert resp.latency_ms > 0
    assert resp.prompt_tokens == 10
    assert resp.stubbed is False


def test_model_property():
    client = ServClient(api_key="x", base_url="http://fake", model="my-model", offline=True)
    assert client.model == "my-model"


def test_reply_sequence():
    """FakeServClient cycles through replies= list."""
    client = FakeServClient(replies=['{"a":1}', '{"b":2}', '{"c":3}'])
    r1 = client.chat(messages=[{"role": "user", "content": "1"}])
    r2 = client.chat(messages=[{"role": "user", "content": "2"}])
    r3 = client.chat(messages=[{"role": "user", "content": "3"}])
    assert r1.content == '{"a":1}'
    assert r2.content == '{"b":2}'
    assert r3.content == '{"c":3}'
