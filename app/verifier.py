"""
app/verifier.py — pure Python, no network calls.

Two public entry points:

  verify(proposal, policy, vaults, amount_usdc) -> VerifierResult
      Checks all nine rules from spec section 5.3.
      Returns pass/fail per rule and a flat list of human-readable problems.

  check_feasibility(policy, vaults, amount_usdc) -> FeasibilityResult
      Grid-searches at 5% steps to see if *any* allocation satisfies the
      policy before the model is called (spec section 5.3, feasibility check).
      Names the conflicting rules when infeasible.

The model never has the last word; this module does.
"""

from __future__ import annotations

import itertools
from typing import Sequence

from app.models import (
    FeasibilityResult,
    Policy,
    Proposal,
    RuleResult,
    Vault,
    VerifierResult,
)

# Tolerances from spec
_WEIGHT_SUM_TOL = 1e-3
_GENERIC_TOL = 1e-6

# Step size for feasibility grid search (spec: 5%)
_GRID_STEP = 0.05


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _vault_map(vaults: Sequence[Vault]) -> dict[str, Vault]:
    return {v.id: v for v in vaults}


def _weighted_avg(values: dict[str, float], weights: dict[str, float]) -> float:
    """Compute a weight-normalised average over the keys present in both dicts."""
    total_w = sum(weights.values())
    if total_w < _GENERIC_TOL:
        return 0.0
    return sum(weights[vid] * values[vid] for vid in weights) / total_w


# ---------------------------------------------------------------------------
# Public: verify
# ---------------------------------------------------------------------------

def verify(
    proposal: Proposal,
    policy: Policy,
    vaults: Sequence[Vault],
    amount_usdc: float,
) -> VerifierResult:
    """
    Check all nine rules from spec section 5.3 against the given proposal.

    Returns a VerifierResult with:
      - passed: True only if every rule passed
      - problems: list of human-readable failure strings
      - checklist: per-rule RuleResult list for the UI
    """
    vault_by_id = _vault_map(vaults)
    allocation = proposal.allocation
    checklist: list[RuleResult] = []
    problems: list[str] = []

    def _pass(rule: str, detail: str = "") -> None:
        checklist.append(RuleResult(rule=rule, passed=True, detail=detail))

    def _fail(rule: str, detail: str) -> None:
        checklist.append(RuleResult(rule=rule, passed=False, detail=detail))
        problems.append(detail)

    # ------------------------------------------------------------------
    # Rule 1: Every vault id exists in the snapshot and is not excluded
    # ------------------------------------------------------------------
    rule = "R1: vault ids valid"
    unknown = [vid for vid in allocation if vid not in vault_by_id]
    excluded_used = [
        vid for vid in allocation
        if vid in policy.excluded_vault_ids and allocation[vid] > _GENERIC_TOL
    ]
    r1_issues: list[str] = []
    if unknown:
        r1_issues.append(f"unknown vault ids: {unknown}")
    if excluded_used:
        r1_issues.append(f"excluded vault ids used: {excluded_used}")
    if r1_issues:
        _fail(rule, "; ".join(r1_issues))
    else:
        _pass(rule)

    # ------------------------------------------------------------------
    # Rule 2: No negative weights; weights sum to 1 (tolerance 1e-3)
    # ------------------------------------------------------------------
    rule = "R2: weights valid"
    negatives = [vid for vid, w in allocation.items() if w < -_GENERIC_TOL]
    weight_sum = sum(allocation.values())
    r2_issues: list[str] = []
    if negatives:
        r2_issues.append(f"negative weights: {negatives}")
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOL:
        r2_issues.append(
            f"weights sum to {weight_sum:.6f}, expected 1.0 ± {_WEIGHT_SUM_TOL}"
        )
    if r2_issues:
        _fail(rule, "; ".join(r2_issues))
    else:
        _pass(rule, f"sum={weight_sum:.6f}")

    # ------------------------------------------------------------------
    # Rule 3: No weight above max_per_vault
    # ------------------------------------------------------------------
    rule = "R3: max_per_vault"
    over = [
        f"{vid}={w:.4f}"
        for vid, w in allocation.items()
        if w > policy.max_per_vault + _GENERIC_TOL
    ]
    if over:
        _fail(rule, f"vaults exceeding max_per_vault={policy.max_per_vault}: {over}")
    else:
        _pass(rule, f"max_per_vault={policy.max_per_vault}")

    # ------------------------------------------------------------------
    # Rule 4: Paused vaults get weight 0
    # ------------------------------------------------------------------
    rule = "R4: no paused vaults"
    paused_with_weight = [
        f"{vid}={allocation[vid]:.4f}"
        for vid, v in vault_by_id.items()
        if vid in allocation and v.paused is True and allocation[vid] > _GENERIC_TOL
    ]
    if paused_with_weight:
        _fail(rule, f"paused vaults with non-zero weight: {paused_with_weight}")
    else:
        _pass(rule)

    # ------------------------------------------------------------------
    # Rule 5: Vaults slower than max_redemption_days get weight 0
    # ------------------------------------------------------------------
    rule = "R5: max_redemption_days"
    if policy.max_redemption_days is not None:
        too_slow = [
            f"{vid}(redemption_days={vault_by_id[vid].redemption_days})"
            for vid in allocation
            if vid in vault_by_id
            and vault_by_id[vid].redemption_days is not None
            and vault_by_id[vid].redemption_days > policy.max_redemption_days  # type: ignore[operator]
            and allocation[vid] > _GENERIC_TOL
        ]
        if too_slow:
            _fail(
                rule,
                f"vaults slower than max_redemption_days={policy.max_redemption_days}: {too_slow}",
            )
        else:
            _pass(rule, f"max_redemption_days={policy.max_redemption_days}")
    else:
        _pass(rule, "no constraint")

    # ------------------------------------------------------------------
    # Rule 6: Liquidity floor
    # min_liquid fraction must be in vaults with redemption_days <= liquid_days
    # Unknown redemption_days counts as NOT liquid
    # ------------------------------------------------------------------
    rule = "R6: liquidity floor"
    liquid_weight = sum(
        w
        for vid, w in allocation.items()
        if vid in vault_by_id
        and vault_by_id[vid].redemption_days is not None
        and vault_by_id[vid].redemption_days <= policy.liquid_days  # type: ignore[operator]
    )
    if liquid_weight < policy.min_liquid - _GENERIC_TOL:
        _fail(
            rule,
            f"liquid weight {liquid_weight:.4f} < min_liquid={policy.min_liquid} "
            f"(liquid_days={policy.liquid_days})",
        )
    else:
        _pass(rule, f"liquid_weight={liquid_weight:.4f}, min_liquid={policy.min_liquid}")

    # ------------------------------------------------------------------
    # Rule 7: Weighted average risk <= max_avg_risk
    # ------------------------------------------------------------------
    rule = "R7: avg risk"
    risk_values = {
        vid: float(vault_by_id[vid].risk)
        for vid in allocation
        if vid in vault_by_id
    }
    active_weights = {vid: w for vid, w in allocation.items() if vid in risk_values}
    avg_risk = _weighted_avg(risk_values, active_weights)
    if avg_risk > policy.max_avg_risk + _GENERIC_TOL:
        _fail(
            rule,
            f"weighted avg risk {avg_risk:.3f} > max_avg_risk={policy.max_avg_risk}",
        )
    else:
        _pass(rule, f"avg_risk={avg_risk:.3f}")

    # ------------------------------------------------------------------
    # Rule 8: Weighted average APY >= min_avg_apy (if set)
    # ------------------------------------------------------------------
    rule = "R8: avg APY"
    if policy.min_avg_apy is not None:
        # Any vault with unknown APY AND non-zero weight causes the rule to fail
        missing_apy = [
            vid for vid in allocation
            if vid in vault_by_id and vault_by_id[vid].apy is None
            and allocation[vid] > _GENERIC_TOL
        ]
        if missing_apy:
            _fail(
                rule,
                f"cannot check min_avg_apy={policy.min_avg_apy} — "
                f"vaults with unknown APY have non-zero weight: {missing_apy}",
            )
        else:
            # Only include vaults that have a known APY and non-zero weight
            apy_values = {
                vid: vault_by_id[vid].apy  # type: ignore[misc]
                for vid in allocation
                if vid in vault_by_id
                and vault_by_id[vid].apy is not None
                and allocation[vid] > _GENERIC_TOL
            }
            apy_weights = {vid: w for vid, w in allocation.items() if vid in apy_values}
            avg_apy = _weighted_avg(apy_values, apy_weights)  # type: ignore[arg-type]
            if avg_apy < policy.min_avg_apy - _GENERIC_TOL:
                _fail(
                    rule,
                    f"weighted avg APY {avg_apy:.4f} < min_avg_apy={policy.min_avg_apy}",
                )
            else:
                _pass(rule, f"avg_apy={avg_apy:.4f}")
    else:
        _pass(rule, "no constraint")

    # ------------------------------------------------------------------
    # Rule 9: Allocation does not exceed available_liquidity_usdc
    # ------------------------------------------------------------------
    rule = "R9: available liquidity cap"
    cap_violations: list[str] = []
    for vid, w in allocation.items():
        if vid not in vault_by_id:
            continue
        cap = vault_by_id[vid].available_liquidity_usdc
        if cap is None:
            continue
        allocated_usdc = w * amount_usdc
        if allocated_usdc > cap + _GENERIC_TOL:
            cap_violations.append(
                f"{vid}: wants {allocated_usdc:.2f} USDC but only {cap:.2f} available"
            )
    if cap_violations:
        _fail(rule, "; ".join(cap_violations))
    else:
        _pass(rule)

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    passed = len(problems) == 0
    return VerifierResult(passed=passed, problems=problems, checklist=checklist)


# ---------------------------------------------------------------------------
# Public: check_feasibility
# ---------------------------------------------------------------------------

def check_feasibility(
    policy: Policy,
    vaults: Sequence[Vault],
    amount_usdc: float,
) -> FeasibilityResult:
    """
    Grid-search at GRID_STEP intervals over all vaults to decide whether ANY
    allocation can satisfy the policy.

    For N <= 6 vaults this is fast enough. For larger N the combinatorial
    explosion is avoided by the early bail-out: the moment one valid allocation
    is found we return feasible=True immediately.

    When infeasible, the returned reason names the rules that could not be met.
    """
    active_vaults = [
        v for v in vaults
        if v.id not in policy.excluded_vault_ids
        and v.paused is not True
        and (
            policy.max_redemption_days is None
            or v.redemption_days is None
            or v.redemption_days <= policy.max_redemption_days
        )
    ]

    if not active_vaults:
        return FeasibilityResult(
            feasible=False,
            reason=(
                "No vaults remain after applying exclusions, paused filter, "
                "and max_redemption_days. Policy cannot be satisfied."
            ),
        )

    n = len(active_vaults)
    steps = int(round(1.0 / _GRID_STEP))
    # Integer steps that sum to `steps` across n vaults
    # We iterate using stars-and-bars: each combo is a tuple of ints
    # This is equivalent to itertools.combinations_with_replacement but
    # we need the actual weight assignments, so we use a recursive generator.

    def _weight_combos(remaining_slots: int, remaining_vaults: int):
        """Yield tuples of integer steps (each * GRID_STEP = weight)."""
        if remaining_vaults == 1:
            yield (remaining_slots,)
            return
        for k in range(0, remaining_slots + 1):
            for rest in _weight_combos(remaining_slots - k, remaining_vaults - 1):
                yield (k,) + rest

    # Track which rules failed in every attempt (to report them)
    rule_failure_counts: dict[str, int] = {}
    total_checked = 0

    for combo in _weight_combos(steps, n):
        weights_float = [k * _GRID_STEP for k in combo]
        allocation = {active_vaults[i].id: weights_float[i] for i in range(n)}
        from app.models import Proposal as _Proposal
        proposal = _Proposal(allocation=allocation, rationale="feasibility check")
        result = verify(proposal, policy, vaults, amount_usdc)
        total_checked += 1
        if result.passed:
            return FeasibilityResult(feasible=True)
        for r in result.checklist:
            if not r.passed:
                rule_failure_counts[r.rule] = rule_failure_counts.get(r.rule, 0) + 1

    # No allocation passed — report the most frequently failing rules
    sorted_failures = sorted(
        rule_failure_counts.items(), key=lambda x: -x[1]
    )
    conflict_rules = ", ".join(r for r, _ in sorted_failures[:3])
    return FeasibilityResult(
        feasible=False,
        reason=(
            f"No allocation satisfies this policy with the available vaults "
            f"(checked {total_checked} combinations at {int(_GRID_STEP*100)}% steps). "
            f"Conflicting rules: {conflict_rules}."
        ),
    )
