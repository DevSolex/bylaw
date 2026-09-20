"""
scripts/e2e_m2.py — live end-to-end M2 check.

Runs three policies through the full parse→propose→verify pipeline
using the real SERV model. Prints every result and token usage.

Usage:
    docker compose run --rm app python scripts/e2e_m2.py

Requires OFFLINE_DEMO=0 and a real SERV_API_KEY in .env.
Exit code 0 if all three scenarios produce the expected outcome.
"""

from __future__ import annotations

import sys
import os
# Ensure the repo root is on the path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone

from app.config import settings
from app.models import Policy, Vault
from app.policy import parse_policy
from app.proposal import propose
from app.serv_client import ServClient

_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

VAULTS = [
    Vault(id="mock-tbill",  name="T-Bill Fund",       chain="ethereum", asset="USDC",
          apy=0.052, redemption_days=0,  risk=1, paused=False, fetched_at=_NOW,
          data_sources={f: "simulated" for f in ["apy","redemption_days","risk"]}),
    Vault(id="mock-bond",   name="IG Bond Vault",      chain="ethereum", asset="USDC",
          apy=0.079, redemption_days=3,  risk=3, paused=False, fetched_at=_NOW,
          data_sources={f: "simulated" for f in ["apy","redemption_days","risk"]}),
    Vault(id="mock-credit", name="Private Credit Pool",chain="ethereum", asset="USDC",
          apy=0.112, redemption_days=30, risk=4, paused=False, fetched_at=_NOW,
          data_sources={f: "simulated" for f in ["apy","redemption_days","risk"]}),
]
AMOUNT = 100_000.0


def hr(label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)


def run_scenario(
    label: str,
    policy_text: str,
    expect_feasible: bool,
    expect_verified: bool | None,  # None means "don't care (infeasible)"
) -> tuple[bool, int]:
    """Returns (scenario_passed, total_tokens_used)."""
    hr(label)
    print(f"  Policy text : {policy_text!r}")
    print(f"  Expected    : feasible={expect_feasible}  verified={expect_verified}")
    print()

    client = ServClient(offline=False)
    call_counter = [0]
    max_calls = settings.serv_max_calls_per_run

    # ── Parse policy ───────────────────────────────────────────────────
    print("── Parsing policy ────────────────────────────────────────────")
    try:
        policy = parse_policy(policy_text, client, call_counter, max_calls)
        print(f"  Parsed      : {policy.model_dump(exclude_defaults=True)}")
    except Exception as exc:
        print(f"  PARSE ERROR : {exc}")
        return False, 0

    # ── Propose ────────────────────────────────────────────────────────
    print("\n── Proposing allocation ──────────────────────────────────────")
    try:
        final, attempts, meta = propose(
            policy, VAULTS, AMOUNT, {}, client, call_counter, max_calls
        )
    except Exception as exc:
        print(f"  PROPOSE ERROR: {exc}")
        return False, 0

    # ── Report ────────────────────────────────────────────────────────
    print(f"  Verified    : {final.verified}")
    if final.allocation:
        for vid, w in final.allocation.items():
            print(f"    {vid}: {w:.2%}")
        print(f"  Rationale   : {final.rationale}")
    if final.reason:
        print(f"  Reason      : {final.reason}")
    print(f"\n  Attempts    : {len(attempts)}")
    for i, a in enumerate(attempts, 1):
        status = "PASS" if a.result.passed else "FAIL"
        print(f"    #{i} verifier: {status}")
        if not a.result.passed:
            for p in a.result.problems:
                print(f"       - {p}")
    print(f"\n  SERV calls  : {meta.total_calls}")
    print(f"  Tokens      : prompt={meta.run_prompt_tokens}  "
          f"completion={meta.run_completion_tokens}  "
          f"total={meta.run_total_tokens}")

    # ── Check outcome ─────────────────────────────────────────────────
    if not expect_feasible:
        ok = not final.verified and len(attempts) == 0
        result_str = "OK (skipped model)" if ok else "UNEXPECTED — model should not have been called"
    elif expect_verified:
        ok = final.verified is True
        result_str = "OK (verified)" if ok else "UNEXPECTED — expected verified allocation"
    else:
        ok = final.verified is False and len(attempts) > 0
        result_str = "OK (verifier caught bad proposal)" if ok else "UNEXPECTED outcome"

    print(f"\n  RESULT      : {result_str}")
    return ok, meta.run_total_tokens


def main() -> None:
    if settings.offline_demo:
        print("ERROR: OFFLINE_DEMO is enabled. Set OFFLINE_DEMO=0 in .env to run live.")
        sys.exit(1)

    print("Bylaw M2 — live end-to-end check")
    print(f"Model: {settings.serv_model}")
    print(f"Call cap per run: {settings.serv_max_calls_per_run}")

    results = []
    total_tokens = 0

    # ── Scenario 1: Feasible policy, expect verified allocation ───────
    ok, tok = run_scenario(
        label="Scenario 1: Feasible policy",
        policy_text=(
            "No more than 40% in any single vault. "
            "Keep at least 20% redeemable within 1 day. "
            "Average risk no higher than 3."
        ),
        expect_feasible=True,
        expect_verified=True,
    )
    results.append(("Scenario 1", ok))
    total_tokens += tok

    # ── Scenario 2: Infeasible — model must be skipped ────────────────
    ok, tok = run_scenario(
        label="Scenario 2: Infeasible policy (model must be skipped)",
        policy_text=(
            "100% must be redeemable within 0 days (instant). "
            "Minimum average APY of 15%."
        ),
        expect_feasible=False,
        expect_verified=None,
    )
    results.append(("Scenario 2", ok))
    total_tokens += tok

    # ── Scenario 3: Tight constraint — model likely overshoots on first try ─
    # max_per_vault=0.5 with 3 vaults is feasible (0.5+0.3+0.2=1.0 works).
    # max_avg_risk=2.0 forces heavy weight on the risk-1 T-Bill vault.
    # min_avg_apy=0.06 rules out a pure T-Bill allocation (APY=5.2% < 6%).
    # This combination is tight enough that a naive "just pick the best APY"
    # model response will fail the risk ceiling, requiring a retry.
    ok, tok = run_scenario(
        label="Scenario 3: Tight risk+APY constraint (verifier should push back)",
        policy_text=(
            "No more than 50% in any single vault. "
            "Average risk score no higher than 2.0. "
            "Minimum average APY of 6%."
        ),
        expect_feasible=True,
        expect_verified=True,
    )
    results.append(("Scenario 3", ok))
    total_tokens += tok

    # ── Summary ───────────────────────────────────────────────────────
    hr("Summary")
    for name, ok in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    print(f"\n  Grand total tokens: {total_tokens}")
    all_ok = all(ok for _, ok in results)
    print(f"\n{'ALL SCENARIOS PASSED' if all_ok else 'SOME SCENARIOS FAILED'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
