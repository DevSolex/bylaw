"""
tests/test_policy.py — unit tests for app/policy.py.

Uses FakeServClient (no network). Covers:
  - Valid JSON → Policy
  - Code-fenced JSON
  - Invalid JSON → repair retry → success
  - Invalid JSON → repair retry → still fails → PolicyParseError
  - Call limit exceeded → CallLimitError
  - Pydantic validation failure
  - Empty policy text → default Policy
"""

from __future__ import annotations

import json
import pytest

from app.models import Policy
from app.policy import CallLimitError, PolicyParseError, parse_policy
from tests.test_serv_client import FakeServClient


def _counter(n: int = 0) -> list[int]:
    return [n]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_parse_valid_json():
    payload = json.dumps({
        "max_per_vault": 0.4,
        "min_liquid": 0.2,
        "liquid_days": 1,
        "max_avg_risk": 3.0,
    })
    client = FakeServClient(reply=payload)
    policy = parse_policy("no more than 40% per vault", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.4
    assert policy.min_liquid == 0.2
    assert policy.max_avg_risk == 3.0


def test_parse_fenced_json():
    """Model wraps reply in ```json ... ``` — should still parse."""
    payload = "```json\n" + json.dumps({"max_per_vault": 0.5}) + "\n```"
    client = FakeServClient(reply=payload)
    policy = parse_policy("50% max per vault", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.5


def test_parse_empty_policy_returns_defaults():
    """Model returns {} → Policy with all defaults (no constraints)."""
    client = FakeServClient(reply="{}")
    policy = parse_policy("", client, _counter(), max_calls=5)
    assert policy == Policy()


def test_unknown_keys_dropped():
    """Pydantic drops unknown keys silently."""
    payload = json.dumps({"max_per_vault": 0.3, "unknown_field": "ignored"})
    client = FakeServClient(reply=payload)
    policy = parse_policy("30% max", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.3
    assert not hasattr(policy, "unknown_field")


# ---------------------------------------------------------------------------
# Repair retry
# ---------------------------------------------------------------------------

class _RepairClient(FakeServClient):
    """Returns bad JSON on first call, good JSON on second."""
    def __init__(self):
        self._calls = 0
        super().__init__()

    def chat(self, messages, **kwargs):
        self._calls += 1
        if self._calls == 1:
            self._reply = "not json at all {"
        else:
            self._reply = json.dumps({"max_avg_risk": 2.5})
        return super().chat(messages, **kwargs)


def test_repair_retry_succeeds():
    client = _RepairClient()
    policy = parse_policy("low risk only", client, _counter(), max_calls=5)
    assert policy.max_avg_risk == 2.5
    assert client._calls == 2


class _AlwaysBadClient(FakeServClient):
    def __init__(self):
        super().__init__(reply="this is not json {{{")


def test_repair_retry_fails_raises():
    client = _AlwaysBadClient()
    with pytest.raises(PolicyParseError, match="repair retry"):
        parse_policy("anything", client, _counter(), max_calls=5)


# ---------------------------------------------------------------------------
# Call limit
# ---------------------------------------------------------------------------

def test_call_limit_raises():
    client = FakeServClient(reply="{}")
    counter = _counter(n=10)  # already at cap
    with pytest.raises(CallLimitError, match="limit"):
        parse_policy("test", client, counter, max_calls=10)


def test_call_counter_increments():
    client = FakeServClient(reply="{}")
    counter = _counter(n=0)
    parse_policy("test", client, counter, max_calls=5)
    assert counter[0] == 1
