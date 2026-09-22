"""
tests/test_policy.py — unit tests for app/policy.py.

Covers:
  - Valid canonical JSON → Policy
  - Code-fenced JSON
  - Alias keys normalised and logged
  - Unknown keys trigger repair retry
  - Invalid JSON → repair retry → success / total failure
  - Call limit exceeded
  - 10 differently-phrased policies (robustness)
  - Parse call counted in shared ModelMeta (accounting)
"""

from __future__ import annotations

import json
import pytest

from app.models import ModelMeta, Policy
from app.policy import CallLimitError, PolicyParseError, _KEY_ALIASES, parse_policy
from tests.test_serv_client import FakeServClient


def _counter(n: int = 0) -> list[int]:
    return [n]


def _meta() -> ModelMeta:
    return ModelMeta(model="fake-model", total_calls=0,
                     run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)


# ---------------------------------------------------------------------------
# Happy paths — canonical keys
# ---------------------------------------------------------------------------

def test_parse_valid_canonical_json():
    payload = json.dumps({"max_per_vault": 0.4, "min_liquid": 0.2,
                           "liquid_days": 1, "max_avg_risk": 3.0})
    client = FakeServClient(reply=payload)
    policy = parse_policy("40% max", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.4
    assert policy.min_liquid == 0.2
    assert policy.max_avg_risk == 3.0


def test_parse_fenced_json():
    payload = "```json\n" + json.dumps({"max_per_vault": 0.5}) + "\n```"
    client = FakeServClient(reply=payload)
    policy = parse_policy("50% max", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.5


def test_empty_policy_returns_defaults():
    client = FakeServClient(reply="{}")
    policy = parse_policy("", client, _counter(), max_calls=5)
    assert policy == Policy()


def test_unknown_keys_trigger_repair_retry_then_succeed():
    """First reply has unknown key → retry → clean reply → parsed."""
    bad  = json.dumps({"max_vault_fraction": 0.3})   # unknown after normalisation
    good = json.dumps({"max_per_vault": 0.3})
    client = FakeServClient(replies=[bad, good])
    policy = parse_policy("30% max", client, _counter(), max_calls=5)
    assert policy.max_per_vault == 0.3
    assert client._call_index == 2   # two calls were made


# ---------------------------------------------------------------------------
# Alias normalisation
# ---------------------------------------------------------------------------

ALIAS_CASES = [
    ("max_vault_allocation",   "max_per_vault",   0.35),
    ("min_liquidity",          "min_liquid",       0.25),
    ("max_redemption_time",    "liquid_days",      2),
    ("max_average_risk",       "max_avg_risk",     3.5),
    ("min_apy",                "min_avg_apy",      0.06),
    ("max_lockup_days",        "max_redemption_days", 7),
]

@pytest.mark.parametrize("alias_key,canonical,value", ALIAS_CASES)
def test_alias_normalised(alias_key, canonical, value):
    payload = json.dumps({alias_key: value})
    client = FakeServClient(reply=payload)
    policy = parse_policy("test", client, _counter(), max_calls=5)
    assert getattr(policy, canonical) == value


# ---------------------------------------------------------------------------
# Repair retry
# ---------------------------------------------------------------------------

def test_repair_retry_on_invalid_json_succeeds():
    client = FakeServClient(replies=["not json {", json.dumps({"max_avg_risk": 2.5})])
    policy = parse_policy("low risk", client, _counter(), max_calls=5)
    assert policy.max_avg_risk == 2.5


def test_repair_retry_always_bad_raises():
    client = FakeServClient(reply="not json {{{")
    with pytest.raises(PolicyParseError, match="repair retry"):
        parse_policy("anything", client, _counter(), max_calls=5)


# ---------------------------------------------------------------------------
# Call limit & accounting
# ---------------------------------------------------------------------------

def test_call_limit_raises():
    client = FakeServClient(reply="{}")
    with pytest.raises(CallLimitError, match="limit"):
        parse_policy("test", client, _counter(10), max_calls=10)


def test_call_counter_increments():
    client = FakeServClient(reply="{}")
    counter = _counter()
    parse_policy("test", client, counter, max_calls=5)
    assert counter[0] == 1


def test_parse_tokens_accumulated_in_meta():
    """Policy-parse SERV call must be reflected in the shared ModelMeta."""
    client = FakeServClient(reply="{}")
    counter = _counter()
    meta = _meta()
    parse_policy("test", client, counter, max_calls=5, meta=meta)
    assert meta.total_calls == 1
    assert meta.run_total_tokens == 15   # FakeServClient returns 15 per call


def test_parse_and_propose_tokens_accumulate():
    """Tokens from parse + propose must both appear in the same ModelMeta."""
    from datetime import datetime, timezone
    from app.models import Policy, Vault
    from app.proposal import propose

    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    vaults = [
        Vault(id="v1", name="V1", chain="eth", asset="USDC",
              risk=2, apy=0.06, redemption_days=1, fetched_at=now),
        Vault(id="v2", name="V2", chain="eth", asset="USDC",
              risk=3, apy=0.08, redemption_days=3, fetched_at=now),
    ]
    valid_alloc = json.dumps({"allocation": {"v1": 0.5, "v2": 0.5}, "rationale": "ok"})

    counter = _counter()
    meta = _meta()
    # Parse call
    parse_client = FakeServClient(reply="{}")
    parse_policy("test", parse_client, counter, max_calls=10, meta=meta)
    assert meta.total_calls == 1

    # Propose call (same counter, same meta)
    propose_client = FakeServClient(reply=valid_alloc)
    propose(Policy(), vaults, 100_000, {}, propose_client, counter, max_calls=10, meta=meta)
    assert meta.total_calls == 2
    assert meta.run_total_tokens == 30   # 15 per call × 2


# ---------------------------------------------------------------------------
# 10-phrasing robustness (fake responses)
# ---------------------------------------------------------------------------

PHRASING_CASES = [
    # (description, policy text, fake model reply, expected field, expected value)
    ("percent sign",
     "Max 40% per vault",
     '{"max_per_vault": 0.40}',
     "max_per_vault", 0.40),

    ("decimal already",
     "Maximum allocation per vault: 0.35",
     '{"max_per_vault": 0.35}',
     "max_per_vault", 0.35),

    ("abbreviation pct",
     "No more than 30pct in any single asset",
     '{"max_per_vault": 0.30}',
     "max_per_vault", 0.30),

    ("risk abbreviation",
     "Avg risk <= 2.5",
     '{"max_avg_risk": 2.5}',
     "max_avg_risk", 2.5),

    ("APY as percentage",
     "Minimum yield 7%",
     '{"min_avg_apy": 0.07}',
     "min_avg_apy", 0.07),

    ("liquid floor with days",
     "Keep 25% redeemable within 2 business days",
     '{"min_liquid": 0.25, "liquid_days": 2}',
     "min_liquid", 0.25),

    ("alias max_average_risk",
     "Average risk of portfolio should not exceed 3",
     '{"max_average_risk": 3.0}',
     "max_avg_risk", 3.0),

    ("alias min_apy",
     "Need at least 6% return on average",
     '{"min_apy": 0.06}',
     "min_avg_apy", 0.06),

    ("alias max_lockup_days",
     "Nothing locked up more than 14 days",
     '{"max_lockup_days": 14}',
     "max_redemption_days", 14),

    ("messy multi-constraint",
     "40% max per vault; keep 20% liquid within 1d; risk under 3; yield at least 7%",
     '{"max_per_vault":0.4,"min_liquid":0.2,"liquid_days":1,"max_avg_risk":3.0,"min_avg_apy":0.07}',
     "max_per_vault", 0.4),
]


@pytest.mark.parametrize("desc,text,reply,field,expected", PHRASING_CASES)
def test_phrasing_robustness(desc, text, reply, field, expected):
    client = FakeServClient(reply=reply)
    policy = parse_policy(text, client, _counter(), max_calls=5)
    assert getattr(policy, field) == expected, f"Failed for: {desc}"


def test_ixs_exact_policy_phrasing():
    """
    Regression: exact IXS demo policy must parse all four constraints.
    Previously the UI showed 'No constraints captured' even though parsing
    worked — root cause was captured_constraints missing from propose response.
    """
    payload = '{"max_per_vault": 0.6, "max_avg_risk": 4.0, "min_avg_apy": 0.065, "max_redemption_days": 7}'
    client = FakeServClient(reply=payload)
    policy = parse_policy(
        "No more than 60% per vault. Avg risk <= 4. Min APY 6.5%. No vaults with redemption > 7 days.",
        client, _counter(), max_calls=5,
    )
    assert policy.max_per_vault == 0.6
    assert policy.max_avg_risk == 4.0
    assert abs(policy.min_avg_apy - 0.065) < 1e-6
    assert policy.max_redemption_days == 7
    # Must not trigger a repair retry
    assert client._call_index == 1


def test_propose_response_includes_captured_constraints():
    """The /api/propose response must include captured_constraints so the UI can display them."""
    import os
    os.environ["OFFLINE_DEMO"] = "1"
    os.environ["DATA_MODE"] = "simulated"
    import app.config as _cfg
    _cfg.settings.offline_demo = True
    _cfg.settings.data_mode = "simulated"
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.post("/api/propose", json={
        "policy": {"max_per_vault": 0.6, "max_avg_risk": 4.0,
                   "min_avg_apy": 0.065, "max_redemption_days": 7},
        "amount_usdc": 500,
    })
    assert r.status_code == 200
    d = r.json()
    assert "captured_constraints" in d, "propose response must include captured_constraints"
    assert "policy" in d, "propose response must include full policy"
