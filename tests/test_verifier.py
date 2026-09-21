"""
tests/test_verifier.py — table-driven tests for app/verifier.py.

Covers every rule (R1–R9), edge cases, and the feasibility check.
No network calls; no API key required.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Policy, Proposal, Vault
from app.verifier import check_feasibility, verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def make_vault(
    id: str = "v1",
    name: str = "Vault 1",
    chain: str = "ethereum",
    asset: str = "USDC",
    apy: float | None = 0.06,
    redemption_days: int | None = 1,
    settlement: str | None = "sync",
    risk: int = 2,
    paused: bool | None = False,
    total_assets_usdc: float | None = None,
    data_sources: dict | None = None,
) -> Vault:
    return Vault(
        id=id,
        name=name,
        chain=chain,
        asset=asset,
        apy=apy,
        redemption_days=redemption_days,
        settlement=settlement,
        risk=risk,
        paused=paused,
        total_assets_usdc=total_assets_usdc,
        data_sources=data_sources or {},
        fetched_at=_NOW,
    )


def make_policy(**kwargs) -> Policy:
    return Policy(**kwargs)


def make_proposal(allocation: dict[str, float], rationale: str = "test") -> Proposal:
    return Proposal(allocation=allocation, rationale=rationale)


# ---------------------------------------------------------------------------
# Two-vault baseline used across many tests
# ---------------------------------------------------------------------------

V1 = make_vault(id="v1", risk=2, apy=0.06, redemption_days=1, paused=False,
                total_assets_usdc=None)
V2 = make_vault(id="v2", risk=4, apy=0.10, redemption_days=7, paused=False,
                total_assets_usdc=None)
VAULTS = [V1, V2]
AMOUNT = 100_000.0


# ===========================================================================
# R1: vault ids valid
# ===========================================================================

def test_r1_pass_known_ids():
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), make_policy(), VAULTS, AMOUNT)
    r1 = next(r for r in result.checklist if r.rule.startswith("R1"))
    assert r1.passed


def test_r1_fail_unknown_vault():
    result = verify(make_proposal({"v1": 0.5, "vX": 0.5}), make_policy(), VAULTS, AMOUNT)
    r1 = next(r for r in result.checklist if r.rule.startswith("R1"))
    assert not r1.passed
    assert "unknown" in r1.detail


def test_r1_fail_excluded_vault_with_weight():
    policy = make_policy(excluded_vault_ids=["v2"])
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r1 = next(r for r in result.checklist if r.rule.startswith("R1"))
    assert not r1.passed
    assert "excluded" in r1.detail


def test_r1_pass_excluded_vault_zero_weight():
    # Excluded vault with weight 0 is fine
    policy = make_policy(excluded_vault_ids=["v2"])
    result = verify(make_proposal({"v1": 1.0, "v2": 0.0}), policy, VAULTS, AMOUNT)
    r1 = next(r for r in result.checklist if r.rule.startswith("R1"))
    assert r1.passed


# ===========================================================================
# R2: weights valid
# ===========================================================================

def test_r2_pass_exact_sum():
    result = verify(make_proposal({"v1": 0.6, "v2": 0.4}), make_policy(), VAULTS, AMOUNT)
    r2 = next(r for r in result.checklist if r.rule.startswith("R2"))
    assert r2.passed


def test_r2_pass_near_one_within_tolerance():
    # 0.9999 — within 1e-3
    result = verify(make_proposal({"v1": 0.5999, "v2": 0.4}), make_policy(), VAULTS, AMOUNT)
    r2 = next(r for r in result.checklist if r.rule.startswith("R2"))
    assert r2.passed


def test_r2_fail_sum_too_low():
    result = verify(make_proposal({"v1": 0.4, "v2": 0.4}), make_policy(), VAULTS, AMOUNT)
    r2 = next(r for r in result.checklist if r.rule.startswith("R2"))
    assert not r2.passed


def test_r2_fail_sum_too_high():
    result = verify(make_proposal({"v1": 0.7, "v2": 0.5}), make_policy(), VAULTS, AMOUNT)
    r2 = next(r for r in result.checklist if r.rule.startswith("R2"))
    assert not r2.passed


def test_r2_fail_negative_weight():
    result = verify(make_proposal({"v1": 1.1, "v2": -0.1}), make_policy(), VAULTS, AMOUNT)
    r2 = next(r for r in result.checklist if r.rule.startswith("R2"))
    assert not r2.passed
    assert "negative" in r2.detail


# ===========================================================================
# R3: max_per_vault
# ===========================================================================

def test_r3_pass():
    policy = make_policy(max_per_vault=0.6)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r3 = next(r for r in result.checklist if r.rule.startswith("R3"))
    assert r3.passed


def test_r3_fail():
    policy = make_policy(max_per_vault=0.4)
    result = verify(make_proposal({"v1": 0.6, "v2": 0.4}), policy, VAULTS, AMOUNT)
    r3 = next(r for r in result.checklist if r.rule.startswith("R3"))
    assert not r3.passed


def test_r3_pass_exactly_at_limit():
    policy = make_policy(max_per_vault=0.5)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r3 = next(r for r in result.checklist if r.rule.startswith("R3"))
    assert r3.passed


# ===========================================================================
# R4: no paused vaults
# ===========================================================================

def test_r4_pass_paused_vault_zero_weight():
    paused_v = make_vault(id="vp", paused=True)
    vaults = [V1, paused_v]
    result = verify(make_proposal({"v1": 1.0, "vp": 0.0}), make_policy(), vaults, AMOUNT)
    r4 = next(r for r in result.checklist if r.rule.startswith("R4"))
    assert r4.passed


def test_r4_fail_paused_vault_nonzero_weight():
    paused_v = make_vault(id="vp", paused=True)
    vaults = [V1, paused_v]
    result = verify(make_proposal({"v1": 0.5, "vp": 0.5}), make_policy(), vaults, AMOUNT)
    r4 = next(r for r in result.checklist if r.rule.startswith("R4"))
    assert not r4.passed


def test_r4_pass_paused_none():
    # paused=None means unknown — not treated as paused
    unknown_pause_v = make_vault(id="vu", paused=None)
    vaults = [V1, unknown_pause_v]
    result = verify(make_proposal({"v1": 0.5, "vu": 0.5}), make_policy(), vaults, AMOUNT)
    r4 = next(r for r in result.checklist if r.rule.startswith("R4"))
    assert r4.passed


# ===========================================================================
# R5: max_redemption_days
# ===========================================================================

def test_r5_pass_no_constraint():
    slow_v = make_vault(id="vs", redemption_days=90)
    vaults = [V1, slow_v]
    result = verify(make_proposal({"v1": 0.5, "vs": 0.5}), make_policy(), vaults, AMOUNT)
    r5 = next(r for r in result.checklist if r.rule.startswith("R5"))
    assert r5.passed


def test_r5_pass_within_limit():
    policy = make_policy(max_redemption_days=7)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r5 = next(r for r in result.checklist if r.rule.startswith("R5"))
    assert r5.passed


def test_r5_fail_too_slow():
    policy = make_policy(max_redemption_days=3)
    # v2 has redemption_days=7
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r5 = next(r for r in result.checklist if r.rule.startswith("R5"))
    assert not r5.passed


def test_r5_pass_slow_vault_zero_weight():
    policy = make_policy(max_redemption_days=3)
    result = verify(make_proposal({"v1": 1.0, "v2": 0.0}), policy, VAULTS, AMOUNT)
    r5 = next(r for r in result.checklist if r.rule.startswith("R5"))
    assert r5.passed


def test_r5_pass_unknown_redemption_not_penalised():
    # Unknown redemption_days is not treated as "too slow" for R5
    unknown_v = make_vault(id="vu", redemption_days=None)
    vaults = [V1, unknown_v]
    policy = make_policy(max_redemption_days=1)
    result = verify(make_proposal({"v1": 0.5, "vu": 0.5}), policy, vaults, AMOUNT)
    r5 = next(r for r in result.checklist if r.rule.startswith("R5"))
    assert r5.passed


# ===========================================================================
# R6: liquidity floor
# ===========================================================================

def test_r6_pass():
    # v1 has redemption_days=1; with liquid_days=1, 50% is liquid
    policy = make_policy(min_liquid=0.4, liquid_days=1)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r6 = next(r for r in result.checklist if r.rule.startswith("R6"))
    assert r6.passed


def test_r6_fail():
    # Require 80% liquid within 1 day, but only 50% in v1
    policy = make_policy(min_liquid=0.8, liquid_days=1)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r6 = next(r for r in result.checklist if r.rule.startswith("R6"))
    assert not r6.passed


def test_r6_unknown_redemption_counts_as_illiquid():
    unknown_v = make_vault(id="vu", redemption_days=None)
    vaults = [V1, unknown_v]
    policy = make_policy(min_liquid=0.6, liquid_days=1)
    # v1=0.5 (liquid), vu=0.5 (unknown, not liquid) => liquid_weight=0.5 < 0.6
    result = verify(make_proposal({"v1": 0.5, "vu": 0.5}), policy, vaults, AMOUNT)
    r6 = next(r for r in result.checklist if r.rule.startswith("R6"))
    assert not r6.passed


def test_r6_instant_counts_as_liquid():
    instant_v = make_vault(id="vi", redemption_days=0)
    vaults = [instant_v, V2]
    policy = make_policy(min_liquid=0.5, liquid_days=0)
    result = verify(make_proposal({"vi": 0.5, "v2": 0.5}), policy, vaults, AMOUNT)
    r6 = next(r for r in result.checklist if r.rule.startswith("R6"))
    assert r6.passed


# ===========================================================================
# R7: avg risk
# ===========================================================================

def test_r7_pass():
    # v1 risk=2, v2 risk=4; 50/50 => avg=3
    policy = make_policy(max_avg_risk=3.0)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r7 = next(r for r in result.checklist if r.rule.startswith("R7"))
    assert r7.passed


def test_r7_fail():
    policy = make_policy(max_avg_risk=2.5)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r7 = next(r for r in result.checklist if r.rule.startswith("R7"))
    assert not r7.passed


def test_r7_pass_heavy_low_risk():
    policy = make_policy(max_avg_risk=2.5)
    # v1 risk=2 (80%), v2 risk=4 (20%) => avg=2.4
    result = verify(make_proposal({"v1": 0.8, "v2": 0.2}), policy, VAULTS, AMOUNT)
    r7 = next(r for r in result.checklist if r.rule.startswith("R7"))
    assert r7.passed


# ===========================================================================
# R8: avg APY
# ===========================================================================

def test_r8_pass_no_constraint():
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), make_policy(), VAULTS, AMOUNT)
    r8 = next(r for r in result.checklist if r.rule.startswith("R8"))
    assert r8.passed


def test_r8_pass_meets_floor():
    # v1 apy=0.06, v2 apy=0.10; avg=0.08
    policy = make_policy(min_avg_apy=0.07)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r8 = next(r for r in result.checklist if r.rule.startswith("R8"))
    assert r8.passed


def test_r8_fail_below_floor():
    policy = make_policy(min_avg_apy=0.09)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    r8 = next(r for r in result.checklist if r.rule.startswith("R8"))
    assert not r8.passed


def test_r8_fail_unknown_apy_with_constraint():
    no_apy_v = make_vault(id="vna", apy=None)
    vaults = [V1, no_apy_v]
    policy = make_policy(min_avg_apy=0.05)
    result = verify(make_proposal({"v1": 0.5, "vna": 0.5}), policy, vaults, AMOUNT)
    r8 = next(r for r in result.checklist if r.rule.startswith("R8"))
    assert not r8.passed
    assert "unknown APY" in r8.detail


def test_r8_pass_unknown_apy_zero_weight():
    # Unknown APY vault with zero weight — should not trigger the rule
    no_apy_v = make_vault(id="vna", apy=None)
    vaults = [V1, no_apy_v]
    policy = make_policy(min_avg_apy=0.05)
    result = verify(make_proposal({"v1": 1.0, "vna": 0.0}), policy, vaults, AMOUNT)
    r8 = next(r for r in result.checklist if r.rule.startswith("R8"))
    assert r8.passed


# ===========================================================================
# R9: available liquidity cap
# ===========================================================================

def test_r9_pass_no_cap():
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), make_policy(), VAULTS, AMOUNT)
    r9 = next(r for r in result.checklist if r.rule.startswith("R9"))
    assert r9.passed


def test_r9_pass_within_cap():
    capped_v = make_vault(id="vc", total_assets_usdc=60_000.0)
    vaults = [capped_v, V2]
    # 50% of 100k = 50k < 60k cap
    result = verify(make_proposal({"vc": 0.5, "v2": 0.5}), make_policy(), vaults, AMOUNT)
    r9 = next(r for r in result.checklist if r.rule.startswith("R9"))
    assert r9.passed


def test_r9_fail_exceeds_cap():
    capped_v = make_vault(id="vc", total_assets_usdc=40_000.0)
    vaults = [capped_v, V2]
    # 50% of 100k = 50k > 40k cap
    result = verify(make_proposal({"vc": 0.5, "v2": 0.5}), make_policy(), vaults, AMOUNT)
    r9 = next(r for r in result.checklist if r.rule.startswith("R9"))
    assert not r9.passed


# ===========================================================================
# Overall pass / fail
# ===========================================================================

def test_all_rules_pass():
    policy = make_policy(
        max_per_vault=0.6,
        min_liquid=0.4,
        liquid_days=1,
        max_avg_risk=3.5,
        min_avg_apy=0.07,
    )
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    assert result.passed
    assert result.problems == []


def test_multiple_failures_reported():
    policy = make_policy(max_per_vault=0.4, max_avg_risk=1.0)
    result = verify(make_proposal({"v1": 0.5, "v2": 0.5}), policy, VAULTS, AMOUNT)
    assert not result.passed
    assert len(result.problems) >= 2


# ===========================================================================
# Feasibility check
# ===========================================================================

def test_feasibility_feasible():
    policy = make_policy(max_avg_risk=5.0, max_per_vault=1.0)
    result = check_feasibility(policy, VAULTS, AMOUNT)
    assert result.feasible


def test_feasibility_infeasible_impossible_risk():
    # Both vaults have risk >= 2, so avg_risk >= 2; require <= 1 → impossible
    policy = make_policy(max_avg_risk=1.0)
    result = check_feasibility(policy, VAULTS, AMOUNT)
    assert not result.feasible
    assert result.reason != ""


def test_feasibility_infeasible_all_excluded():
    policy = make_policy(excluded_vault_ids=["v1", "v2"])
    result = check_feasibility(policy, VAULTS, AMOUNT)
    assert not result.feasible


def test_feasibility_infeasible_all_paused():
    paused_vaults = [
        make_vault(id="v1", paused=True),
        make_vault(id="v2", paused=True),
    ]
    result = check_feasibility(make_policy(), paused_vaults, AMOUNT)
    assert not result.feasible


def test_feasibility_infeasible_names_conflict_rules():
    # Require 100% instant liquid AND min_apy=0.15 — impossible
    policy = make_policy(min_liquid=1.0, liquid_days=0, min_avg_apy=0.15)
    result = check_feasibility(policy, VAULTS, AMOUNT)
    assert not result.feasible
    # New LP-based message uses "Conflicting constraints" not "Conflicting rules"
    assert "infeasible" in result.reason.lower() or "cannot be satisfied" in result.reason.lower()
    assert result.reason != ""


def test_feasibility_feasible_single_vault():
    single_vault = [make_vault(id="v1", risk=2, apy=0.06, redemption_days=0)]
    policy = make_policy(max_per_vault=1.0, min_liquid=1.0, liquid_days=0)
    result = check_feasibility(policy, single_vault, AMOUNT)
    assert result.feasible


def test_feasibility_respects_liquidity_cap():
    # Cap is 40k, amount is 100k, so no single-vault allocation can put > 40% in the capped vault
    # With max_per_vault=1.0 this might still be feasible if we can go 40/60 split
    capped_v = make_vault(id="vc", total_assets_usdc=40_000.0, risk=2)
    other_v = make_vault(id="vo", risk=2)
    vaults = [capped_v, other_v]
    policy = make_policy()  # no tight constraints
    result = check_feasibility(policy, vaults, AMOUNT)
    assert result.feasible


# ===========================================================================
# Data model validation
# ===========================================================================

def test_policy_defaults():
    p = Policy()
    assert p.max_per_vault == 1.0
    assert p.min_liquid == 0.0
    assert p.max_avg_risk == 5.0
    assert p.min_avg_apy is None
    assert p.excluded_vault_ids == []


def test_vault_risk_bounds():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        make_vault(risk=0)
    with pytest.raises(pydantic.ValidationError):
        make_vault(risk=6)


def test_vault_risk_valid_range():
    for r in range(1, 6):
        v = make_vault(risk=r)
        assert v.risk == r
