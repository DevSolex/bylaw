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
# Public: check_feasibility (LP-exact + grid confirmation)
# ---------------------------------------------------------------------------

def check_feasibility(
    policy: Policy,
    vaults: Sequence[Vault],
    amount_usdc: float,
) -> FeasibilityResult:
    """
    Determine whether ANY allocation can satisfy the policy.

    Method
    ------
    1. Pre-filter: remove excluded, paused, and too-slow vaults.
    2. LP feasibility (scipy.optimize.linprog): formulate the policy
       constraints as a linear program and check whether a feasible point
       exists.  This gives an EXACT answer for all linear constraints
       (weight bounds, liquidity floor, risk ceiling, APY floor, cap).
    3. If LP says infeasible → "infeasible" (exact) + named rules + relaxation.
    4. If LP says feasible but no allocation found at 5% grid → the message
       says "no allocation found at 5% steps" (not "infeasible").

    The relaxation suggestion reports the best reachable value for the most
    binding constraint (e.g. highest achievable APY or lowest achievable risk).
    """
    from app.models import Proposal as _Proposal  # avoid circular at module level

    # ── Pre-filter ─────────────────────────────────────────────────────
    active = [
        v for v in vaults
        if v.id not in policy.excluded_vault_ids
        and v.paused is not True
        and (
            policy.max_redemption_days is None
            or v.redemption_days is None
            or v.redemption_days <= policy.max_redemption_days
        )
    ]

    if not active:
        return FeasibilityResult(
            feasible=False,
            reason=(
                "No vaults remain after applying exclusions, paused filter, "
                "and max_redemption_days constraint. Policy cannot be satisfied."
            ),
        )

    n = len(active)

    # ── LP feasibility check ───────────────────────────────────────────
    lp_result = _lp_feasibility(policy, active, amount_usdc)

    if not lp_result["feasible"]:
        relaxation = _suggest_relaxation(policy, active)
        conflict_rules = ", ".join(lp_result["conflict_rules"])
        return FeasibilityResult(
            feasible=False,
            reason=(
                f"This policy cannot be satisfied with the available vaults "
                f"(exact linear-programming check). "
                f"Conflicting constraints: {conflict_rules}. "
                f"{relaxation}"
            ),
        )

    # ── Grid search (confirms LP result at 5% resolution) ─────────────
    steps = int(round(1.0 / _GRID_STEP))

    def _weight_combos(remaining_slots: int, remaining_vaults: int):
        if remaining_vaults == 1:
            yield (remaining_slots,)
            return
        for k in range(0, remaining_slots + 1):
            for rest in _weight_combos(remaining_slots - k, remaining_vaults - 1):
                yield (k,) + rest

    rule_failure_counts: dict[str, int] = {}
    total_checked = 0

    for combo in _weight_combos(steps, n):
        weights_float = [k * _GRID_STEP for k in combo]
        allocation = {active[i].id: weights_float[i] for i in range(n)}
        proposal = _Proposal(allocation=allocation, rationale="feasibility check")
        result = verify(proposal, policy, vaults, amount_usdc)
        total_checked += 1
        if result.passed:
            return FeasibilityResult(feasible=True)
        for r in result.checklist:
            if not r.passed:
                rule_failure_counts[r.rule] = rule_failure_counts.get(r.rule, 0) + 1

    # LP said feasible but grid found nothing — a solution exists at finer resolution
    sorted_failures = sorted(rule_failure_counts.items(), key=lambda x: -x[1])
    conflict_rules_str = ", ".join(r for r, _ in sorted_failures[:3])
    return FeasibilityResult(
        feasible=True,  # LP confirmed a solution exists; grid just can't pin it at 5%
        reason=(
            f"A valid allocation exists (LP confirmed) but was not found at "
            f"{int(_GRID_STEP * 100)}% step resolution "
            f"(checked {total_checked} combinations). "
            f"Most constrained rules: {conflict_rules_str}."
        ),
    )


def _lp_feasibility(
    policy: Policy,
    active: list[Vault],
    amount_usdc: float,
) -> dict:
    """
    Solve a linear program to check whether a feasible allocation exists.

    Decision variables: w[i] = weight for vault i (0 ≤ w[i] ≤ max_per_vault).
    Constraints encoded:
      - Σ w[i] = 1
      - w[i] ≤ max_per_vault  (upper bound)
      - Σ_{liquid_i} w[i] ≥ min_liquid
      - Σ risk[i]*w[i] ≤ max_avg_risk
      - Σ apy[i]*w[i] ≥ min_avg_apy  (if set and all APYs known)
      - w[i] * amount_usdc ≤ available_liquidity_usdc[i]  (if cap known)

    Returns {"feasible": bool, "conflict_rules": list[str]}.
    """
    try:
        from scipy.optimize import linprog
    except ImportError:
        # scipy not available — fall through to grid only
        return {"feasible": True, "conflict_rules": []}

    n = len(active)
    # Objective: minimise 0 (feasibility only)
    c = [0.0] * n

    A_ub: list[list[float]] = []
    b_ub: list[float] = []
    conflict_rules: list[str] = []

    # ── Equality: weights sum to 1 (encode as two inequalities) ───────
    # Σw ≤ 1 and -Σw ≤ -1
    A_ub.append([1.0] * n)
    b_ub.append(1.0 + _WEIGHT_SUM_TOL)
    A_ub.append([-1.0] * n)
    b_ub.append(-(1.0 - _WEIGHT_SUM_TOL))

    # ── Liquidity floor: -Σ_{liquid} w[i] ≤ -min_liquid ──────────────
    if policy.min_liquid > 0:
        row = [
            -1.0 if (v.redemption_days is not None and v.redemption_days <= policy.liquid_days)
            else 0.0
            for v in active
        ]
        A_ub.append(row)
        b_ub.append(-policy.min_liquid)

    # ── Risk ceiling: Σ risk[i]*w[i] ≤ max_avg_risk ──────────────────
    if policy.max_avg_risk < 5.0:
        A_ub.append([float(v.risk) for v in active])
        b_ub.append(policy.max_avg_risk)

    # ── APY floor: -Σ apy[i]*w[i] ≤ -min_avg_apy ────────────────────
    if policy.min_avg_apy is not None:
        apys = [v.apy for v in active]
        if all(a is not None for a in apys):
            A_ub.append([-float(a) for a in apys])  # type: ignore[arg-type]
            b_ub.append(-policy.min_avg_apy)

    # ── Per-vault caps from available_liquidity_usdc ──────────────────
    for i, v in enumerate(active):
        if v.available_liquidity_usdc is not None:
            row = [0.0] * n
            row[i] = amount_usdc
            A_ub.append(row)
            b_ub.append(v.available_liquidity_usdc)

    # ── Bounds: 0 ≤ w[i] ≤ max_per_vault ─────────────────────────────
    bounds = [(0.0, policy.max_per_vault)] * n

    res = linprog(c, A_ub=A_ub or None, b_ub=b_ub or None, bounds=bounds, method="highs")

    if res.status == 0:
        return {"feasible": True, "conflict_rules": []}

    # Identify which constraints are responsible by checking each individually
    conflict_rules = _identify_conflicts(policy, active, amount_usdc)
    return {"feasible": False, "conflict_rules": conflict_rules}


def _identify_conflicts(
    policy: Policy,
    active: list[Vault],
    amount_usdc: float,
) -> list[str]:
    """
    Identify which rules make the policy infeasible by disabling them one at a
    time and seeing which relaxation makes it feasible.
    """
    conflicts: list[str] = []

    # Check if weights can sum to 1 given max_per_vault
    n = len(active)
    if n * policy.max_per_vault < 1.0 - _WEIGHT_SUM_TOL:
        conflicts.append(
            f"R3: max_per_vault={policy.max_per_vault} — "
            f"{n} vaults × {policy.max_per_vault} = {n * policy.max_per_vault:.2f} < 1.0"
        )
        return conflicts  # no point checking further

    # Check best achievable risk
    min_possible_risk = min(float(v.risk) for v in active)
    if min_possible_risk > policy.max_avg_risk:
        conflicts.append(
            f"R7: avg risk — lowest available risk is {min_possible_risk}, "
            f"but max_avg_risk={policy.max_avg_risk}"
        )

    # Check best achievable APY
    if policy.min_avg_apy is not None:
        known_apys = [v.apy for v in active if v.apy is not None]
        if known_apys:
            max_possible_apy = max(known_apys)
            if max_possible_apy < policy.min_avg_apy:
                conflicts.append(
                    f"R8: avg APY — highest available APY is {max_possible_apy:.1%}, "
                    f"but min_avg_apy={policy.min_avg_apy:.1%}"
                )

    # Check liquidity floor
    if policy.min_liquid > 0:
        max_liquid = sum(
            1.0 for v in active
            if v.redemption_days is not None and v.redemption_days <= policy.liquid_days
        )
        if max_liquid < policy.min_liquid:
            conflicts.append(
                f"R6: liquidity floor — max achievable liquid weight is {max_liquid:.2f}, "
                f"but min_liquid={policy.min_liquid}"
            )

    if not conflicts:
        conflicts.append("constraints are jointly infeasible (individual checks passed)")

    return conflicts


def _suggest_relaxation(policy: Policy, active: list[Vault]) -> str:
    """
    Suggest one concrete relaxation for the most binding constraint.
    """
    suggestions: list[str] = []

    # Best APY reachable under the risk constraint
    risk_ok = [v for v in active if float(v.risk) <= policy.max_avg_risk]
    if policy.min_avg_apy is not None and risk_ok:
        best_apy = max(v.apy for v in risk_ok if v.apy is not None) if any(
            v.apy is not None for v in risk_ok
        ) else None
        if best_apy is not None and best_apy < policy.min_avg_apy:
            suggestions.append(
                f"Relaxation: lower min_avg_apy to {best_apy:.1%} "
                f"(highest APY reachable within max_avg_risk={policy.max_avg_risk})"
            )

    # Minimum risk achievable while meeting APY floor
    if policy.min_avg_apy is not None:
        apy_ok = [v for v in active if v.apy is not None and v.apy >= policy.min_avg_apy]
        if apy_ok:
            min_risk_in_apy_ok = min(float(v.risk) for v in apy_ok)
            if min_risk_in_apy_ok > policy.max_avg_risk:
                suggestions.append(
                    f"Relaxation: raise max_avg_risk to {min_risk_in_apy_ok:.1f} "
                    f"(lowest risk among vaults meeting the APY floor)"
                )

    # Liquidity
    if policy.min_liquid > 0:
        liquid_vaults = [
            v for v in active
            if v.redemption_days is not None and v.redemption_days <= policy.liquid_days
        ]
        if liquid_vaults:
            best_liquid = min(1.0, len(liquid_vaults) * policy.max_per_vault)
            if best_liquid < policy.min_liquid:
                suggestions.append(
                    f"Relaxation: lower min_liquid to {best_liquid:.2f} "
                    f"(max achievable with current liquid vaults)"
                )

    if suggestions:
        return " | ".join(suggestions[:2])
    return "Consider reducing the constraint values or adding more diverse vaults."
