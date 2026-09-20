"""
tests/test_proposal.py — unit tests for app/proposal.py.

Covers:
  - Feasible policy → verified on first attempt
  - Verifier rejects → retry → success
  - All 3 attempts fail → RunFinal(verified=False), never shown as recommendation
  - Infeasible → model never called
  - Parse error counts as failed attempt
  - Call limit hit mid-loop
  - Token totals accumulate across attempts
  - Repair loop: deliberately broken allocation → problems fed back → corrected
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.models import ModelMeta, Policy, RunFinal, Vault
from app.proposal import propose
from tests.test_serv_client import FakeServClient

_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def make_vault(id, risk=2, apy=0.06, redemption_days=1, paused=False,
               available_liquidity_usdc=None):
    return Vault(
        id=id, name=id, chain="ethereum", asset="USDC",
        apy=apy, redemption_days=redemption_days, risk=risk,
        paused=paused, available_liquidity_usdc=available_liquidity_usdc,
        fetched_at=_NOW,
    )


VAULTS = [
    make_vault("v-tbill",  risk=1, apy=0.05, redemption_days=0),
    make_vault("v-bond",   risk=3, apy=0.08, redemption_days=3),
    make_vault("v-credit", risk=4, apy=0.11, redemption_days=30),
]
AMOUNT = 100_000.0

_VALID_ALLOC = json.dumps({
    "allocation": {"v-tbill": 0.5, "v-bond": 0.3, "v-credit": 0.2},
    "rationale": "Balanced allocation.",
})

# Breaks R3 max_per_vault=0.4
_OVER_CAP_ALLOC = json.dumps({
    "allocation": {"v-tbill": 0.8, "v-bond": 0.1, "v-credit": 0.1},
    "rationale": "Too much in tbill.",
})


def _meta():
    return ModelMeta(model="fake", total_calls=0,
                     run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_verified_first_attempt():
    client = FakeServClient(reply=_VALID_ALLOC)
    final, attempts, meta = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is True
    assert len(attempts) == 1
    assert abs(sum(final.allocation.values()) - 1.0) < 1e-6


def test_token_meta_populated():
    client = FakeServClient(reply=_VALID_ALLOC)
    _, _, meta = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], 10)
    assert meta.total_calls == 1
    assert meta.run_total_tokens == 15


# ---------------------------------------------------------------------------
# Repair loop: broken → fix
# ---------------------------------------------------------------------------

def test_repair_loop_verifier_rejects_then_corrects():
    """First reply breaks per-vault cap; second reply is valid within cap."""
    policy = Policy(max_per_vault=0.4)
    # This allocation respects max_per_vault=0.4
    capped_valid = json.dumps({
        "allocation": {"v-tbill": 0.4, "v-bond": 0.4, "v-credit": 0.2},
        "rationale": "Within cap.",
    })
    client = FakeServClient(replies=[_OVER_CAP_ALLOC, capped_valid])
    final, attempts, meta = propose(policy, VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is True
    assert len(attempts) == 2
    assert attempts[0].result.passed is False
    assert attempts[1].result.passed is True
    assert meta.total_calls == 2
    assert meta.run_total_tokens == 30
    # The second prompt must contain the verifier's problem description
    second_call_messages = client.all_calls[1]
    user_msgs = [m["content"] for m in second_call_messages if m["role"] == "user"]
    combined = " ".join(user_msgs)
    assert "previous allocation failed" in combined or "max_per_vault" in combined


# ---------------------------------------------------------------------------
# All attempts fail → shown as failed, never as a recommendation
# ---------------------------------------------------------------------------

def test_all_attempts_fail_is_unverified():
    bad = json.dumps({
        "allocation": {"v-tbill": 0.1, "v-bond": 0.1, "v-credit": 0.1},
        "rationale": "sums to 0.3 — always fails R2",
    })
    client = FakeServClient(reply=bad)
    final, attempts, meta = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is False
    assert final.allocation is None   # NEVER shown as allocation
    assert len(final.reason) > 0
    assert len(attempts) == 3
    assert meta.total_calls == 3


def test_failed_final_has_reason_not_allocation():
    """Spec requirement: unverified proposals must not reach the approval screen."""
    bad = json.dumps({"allocation": {"v-tbill": 0.2}, "rationale": "incomplete"})
    client = FakeServClient(reply=bad)
    final, _, _ = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is False
    assert final.allocation is None
    assert "failed" in final.reason.lower() or "problem" in final.reason.lower() or \
           "infeasible" in final.reason.lower()


# ---------------------------------------------------------------------------
# Infeasible — model never called
# ---------------------------------------------------------------------------

def test_infeasible_skips_model():
    impossible = Policy(min_liquid=1.0, liquid_days=0, min_avg_apy=0.99)
    client = FakeServClient(reply=_VALID_ALLOC)
    final, attempts, meta = propose(impossible, VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is False
    assert attempts == []
    assert meta.total_calls == 0


# ---------------------------------------------------------------------------
# Parse error counts as attempt
# ---------------------------------------------------------------------------

def test_parse_error_uses_retry_slot():
    client = FakeServClient(replies=["not json", _VALID_ALLOC])
    final, attempts, meta = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], 10)
    assert final.verified is True
    assert len(attempts) == 1   # parse error not added to attempts list


# ---------------------------------------------------------------------------
# Call limit
# ---------------------------------------------------------------------------

def test_call_limit_returns_unverified():
    client = FakeServClient(reply=_VALID_ALLOC)
    final, _, _ = propose(Policy(), VAULTS, AMOUNT, {}, client, [10], max_calls=10)
    assert final.verified is False
    assert "limit" in final.reason.lower()


# ---------------------------------------------------------------------------
# Shared meta accounting (parse + propose in one run)
# ---------------------------------------------------------------------------

def test_shared_meta_parse_plus_propose():
    """Parse tokens + propose tokens both appear in the single meta."""
    from app.policy import parse_policy

    counter = [0]
    meta = _meta()
    parse_client = FakeServClient(reply='{"max_per_vault": 0.5}')
    parse_policy("50% max", parse_client, counter, max_calls=10, meta=meta)

    propose_client = FakeServClient(reply=_VALID_ALLOC)
    propose(Policy(), VAULTS, AMOUNT, {}, propose_client, counter, max_calls=10, meta=meta)

    # 2 SERV calls total (1 parse + 1 propose), 15 tokens each = 30 total
    assert meta.total_calls == 2
    assert meta.run_total_tokens == 30
    assert counter[0] == 2
