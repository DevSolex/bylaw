"""
tests/test_proposal.py — unit tests for app/proposal.py.

Uses FakeServClient (no network). Covers:
  - Feasible policy → verified allocation on first attempt
  - Verifier rejects first proposal → succeeds on second attempt
  - All 3 attempts fail → RunFinal(verified=False)
  - Infeasible policy → model never called, RunFinal(verified=False)
  - Parse error on first attempt → retry
  - Call limit hit mid-loop → RunFinal(verified=False)
  - Token totals accumulate across attempts
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.models import Policy, Vault, RunFinal
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
    make_vault("v-tbill", risk=1, apy=0.05, redemption_days=0),
    make_vault("v-bond",  risk=3, apy=0.08, redemption_days=3),
    make_vault("v-credit",risk=4, apy=0.11, redemption_days=30),
]
AMOUNT = 100_000.0

# A valid allocation for the three vaults (passes all default-policy rules)
_VALID_ALLOC = json.dumps({
    "allocation": {"v-tbill": 0.5, "v-bond": 0.3, "v-credit": 0.2},
    "rationale": "Balanced allocation respecting risk and liquidity constraints.",
})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_feasible_verified_first_attempt():
    client = FakeServClient(reply=_VALID_ALLOC)
    final, attempts, meta = propose(
        Policy(), VAULTS, AMOUNT, {}, client, [0], max_calls=10
    )
    assert final.verified is True
    assert len(attempts) == 1
    assert abs(sum(final.allocation.values()) - 1.0) < 1e-6


def test_token_meta_populated():
    client = FakeServClient(reply=_VALID_ALLOC)
    _, _, meta = propose(Policy(), VAULTS, AMOUNT, {}, client, [0], max_calls=10)
    assert meta.total_calls == 1
    assert meta.run_prompt_tokens == 10   # FakeServClient returns 10
    assert meta.run_completion_tokens == 5
    assert meta.run_total_tokens == 15


# ---------------------------------------------------------------------------
# Retry on verifier failure
# ---------------------------------------------------------------------------

class _FailThenPassClient(FakeServClient):
    """Returns an invalid allocation first, then the valid one."""
    def __init__(self):
        self._calls = 0
        super().__init__()

    def chat(self, messages, **kwargs):
        self._calls += 1
        if self._calls == 1:
            # Weights don't sum to 1 → R2 fails
            self._reply = json.dumps({
                "allocation": {"v-tbill": 0.3, "v-bond": 0.3, "v-credit": 0.2},
                "rationale": "bad",
            })
        else:
            self._reply = _VALID_ALLOC
        return super().chat(messages, **kwargs)


def test_verifier_retry_succeeds_on_second_attempt():
    client = _FailThenPassClient()
    final, attempts, meta = propose(
        Policy(), VAULTS, AMOUNT, {}, client, [0], max_calls=10
    )
    assert final.verified is True
    assert len(attempts) == 2
    assert attempts[0].result.passed is False
    assert attempts[1].result.passed is True
    assert meta.total_calls == 2
    assert meta.run_total_tokens == 30   # 15 × 2 calls


# ---------------------------------------------------------------------------
# All attempts fail
# ---------------------------------------------------------------------------

class _AlwaysFailClient(FakeServClient):
    def __init__(self):
        super().__init__(reply=json.dumps({
            "allocation": {"v-tbill": 0.1, "v-bond": 0.1, "v-credit": 0.1},
            "rationale": "always fails sum",
        }))


def test_all_attempts_fail_returns_unverified():
    client = _AlwaysFailClient()
    final, attempts, meta = propose(
        Policy(), VAULTS, AMOUNT, {}, client, [0], max_calls=10
    )
    assert final.verified is False
    assert "failed verification" in final.reason
    assert len(attempts) == 3  # all three attempts recorded
    assert meta.total_calls == 3


# ---------------------------------------------------------------------------
# Infeasible policy — model never called
# ---------------------------------------------------------------------------

def test_infeasible_policy_skips_model():
    # Require 100% instant-liquid AND min APY of 99% — impossible with these vaults
    impossible_policy = Policy(min_liquid=1.0, liquid_days=0, min_avg_apy=0.99)
    client = FakeServClient(reply=_VALID_ALLOC)
    final, attempts, meta = propose(
        impossible_policy, VAULTS, AMOUNT, {}, client, [0], max_calls=10
    )
    assert final.verified is False
    assert attempts == []        # model never called
    assert meta.total_calls == 0


# ---------------------------------------------------------------------------
# Parse error on first proposal attempt
# ---------------------------------------------------------------------------

class _BadJsonThenGoodClient(FakeServClient):
    def __init__(self):
        self._calls = 0
        super().__init__()

    def chat(self, messages, **kwargs):
        self._calls += 1
        self._reply = "not json" if self._calls == 1 else _VALID_ALLOC
        return super().chat(messages, **kwargs)


def test_parse_error_counts_as_failed_attempt():
    client = _BadJsonThenGoodClient()
    final, attempts, meta = propose(
        Policy(), VAULTS, AMOUNT, {}, client, [0], max_calls=10
    )
    # Parse error on attempt 1 (not added to attempts list),
    # valid on attempt 2
    assert final.verified is True
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Call limit
# ---------------------------------------------------------------------------

def test_call_limit_hit_returns_unverified():
    client = FakeServClient(reply=_VALID_ALLOC)
    counter = [9]  # 9 calls already used; cap is 10; one more allowed but
    # we want to see what happens when counter starts at cap
    counter = [10]
    final, attempts, meta = propose(
        Policy(), VAULTS, AMOUNT, {}, client, counter, max_calls=10
    )
    assert final.verified is False
    assert "limit" in final.reason
