"""
scripts/e2e_pre_m3.py — live checks for pre-M3 items.

1. Repair-loop check: inject a bad first allocation (over per-vault cap),
   confirm the model receives the violation list and produces a valid fix.
2. Accounting: confirm parse tokens + propose tokens both show in Run totals.
3. 10-phrasing live batch: parse 10 differently-phrased policies with the
   real model, report which aliases appeared and what was captured.

Usage:
    docker compose run --rm app python scripts/e2e_pre_m3.py
Requires OFFLINE_DEMO=0 and a real SERV_API_KEY.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
import json

from app.config import settings
from app.models import ModelMeta, Policy, Vault
from app.policy import parse_policy, _KEY_ALIASES
from app.proposal import propose
from app.serv_client import ServClient

_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

VAULTS = [
    Vault(id="mock-tbill",  name="T-Bill Fund",        chain="ethereum", asset="USDC",
          apy=0.052, redemption_days=0,  risk=1, paused=False, fetched_at=_NOW,
          data_sources={f:"simulated" for f in ["apy","redemption_days","risk"]}),
    Vault(id="mock-bond",   name="IG Bond Vault",       chain="ethereum", asset="USDC",
          apy=0.079, redemption_days=3,  risk=3, paused=False, fetched_at=_NOW,
          data_sources={f:"simulated" for f in ["apy","redemption_days","risk"]}),
    Vault(id="mock-credit", name="Private Credit Pool", chain="ethereum", asset="USDC",
          apy=0.112, redemption_days=30, risk=4, paused=False, fetched_at=_NOW,
          data_sources={f:"simulated" for f in ["apy","redemption_days","risk"]}),
]
AMOUNT = 100_000.0


def hr(s): print(f"\n{'='*60}\n  {s}\n{'='*60}")


def make_meta():
    return ModelMeta(model=settings.serv_model, total_calls=0,
                     run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)


# ---------------------------------------------------------------------------
# CHECK 1: Repair loop with deliberately invalid first proposal
# ---------------------------------------------------------------------------

class _BadFirstClient(ServClient):
    """
    Returns a hardcoded over-cap allocation on first proposal call,
    then delegates to the real API for the repair attempt.
    """
    _proposal_calls = 0

    def chat(self, messages, **kwargs):
        content = messages[-1]["content"] if messages else ""
        if "Propose an allocation" in content or "VAULT SNAPSHOT" in content:
            self._proposal_calls += 1
            if self._proposal_calls == 1:
                # Deliberately invalid: mock-tbill gets 0.85 > any reasonable cap
                bad = json.dumps({
                    "allocation": {
                        "mock-tbill": 0.85,
                        "mock-bond":  0.10,
                        "mock-credit": 0.05,
                    },
                    "rationale": "[INJECTED BAD] Over per-vault cap intentionally.",
                })
                from app.serv_client import ServResponse
                return ServResponse(content=bad, model=self._model,
                                    latency_ms=0, prompt_tokens=5,
                                    completion_tokens=5, total_tokens=10)
        return super().chat(messages, **kwargs)


def check_repair_loop():
    hr("Check 1: Repair loop (bad first proposal → live model repair)")
    policy = Policy(max_per_vault=0.5)
    client = _BadFirstClient(offline=False)
    counter = [0]
    meta = make_meta()

    # Parse
    parse_policy("max 50% per vault", client, counter, max_calls=10, meta=meta)
    print(f"  After parse  — calls={meta.total_calls}  tokens={meta.run_total_tokens}")

    # Propose (first attempt injected bad, second goes live)
    final, attempts, meta = propose(policy, VAULTS, AMOUNT, {}, client, counter, 10, meta)

    print(f"  Attempts     : {len(attempts)}")
    for i, a in enumerate(attempts, 1):
        status = "PASS" if a.result.passed else "FAIL"
        print(f"    #{i}: {status}")
        if not a.result.passed:
            for p in a.result.problems:
                print(f"       - {p}")
    print(f"  Verified     : {final.verified}")
    if final.allocation:
        for vid, w in final.allocation.items():
            print(f"    {vid}: {w:.2%}")
    if final.reason:
        print(f"  Reason       : {final.reason}")
    print(f"  Total calls  : {meta.total_calls}")
    print(f"  Total tokens : prompt={meta.run_prompt_tokens} "
          f"completion={meta.run_completion_tokens} total={meta.run_total_tokens}")

    ok = final.verified and len(attempts) >= 2 and attempts[0].result.passed is False
    print(f"\n  RESULT: {'PASS — verifier caught bad proposal, model repaired it' if ok else 'UNEXPECTED'}")
    return ok, meta.run_total_tokens


# ---------------------------------------------------------------------------
# CHECK 2: Token accounting — parse + propose both counted
# ---------------------------------------------------------------------------

def check_accounting():
    hr("Check 2: Token accounting (parse + propose in one Run)")
    client = ServClient(offline=False)
    counter = [0]
    meta = make_meta()

    policy = parse_policy(
        "No more than 40% in any vault. Average risk no higher than 3.",
        client, counter, max_calls=10, meta=meta,
    )
    parse_calls = meta.total_calls
    parse_tokens = meta.run_total_tokens
    print(f"  After parse  — calls={parse_calls}  tokens={parse_tokens}")

    final, attempts, meta = propose(
        policy, VAULTS, AMOUNT, {}, client, counter, 10, meta
    )
    print(f"  After propose — calls={meta.total_calls}  tokens={meta.run_total_tokens}")
    print(f"  Verified     : {final.verified}")

    ok = meta.total_calls >= 2 and meta.run_total_tokens > parse_tokens
    print(f"\n  RESULT: {'PASS — parse tokens included in Run total' if ok else 'FAIL — parse not counted'}")
    return ok, meta.run_total_tokens


# ---------------------------------------------------------------------------
# CHECK 3: 10-phrasing live batch
# ---------------------------------------------------------------------------

PHRASINGS = [
    "Max 40% per vault",
    "No more than 0.35 in any single vault",
    "Keep 20% redeemable within 1 day",
    "Average risk of portfolio should not exceed 3",
    "Minimum yield 7%",
    "Nothing locked up more than 14 days",
    "40% max per vault; average risk under 2.5; yield at least 6%",
    "Risk ceiling 3.5, liquidity floor 25% within 2 days",
    "max_per_vault=0.3, min_avg_apy=0.08",
    "Spread evenly, risk <= 3, APY >= 5%",
]


def check_phrasing_batch():
    hr("Check 3: 10-phrasing live batch")
    client = ServClient(offline=False)
    all_aliases: list[str] = []
    total_tokens = 0

    for i, text in enumerate(PHRASINGS, 1):
        counter = [0]
        meta = make_meta()
        try:
            policy = parse_policy(text, client, counter, max_calls=5, meta=meta)
            captured = policy.model_dump(exclude_defaults=True)
        except Exception as e:
            captured = f"ERROR: {e}"
        total_tokens += meta.run_total_tokens
        print(f"  [{i:02d}] {text!r}")
        print(f"        → {captured}  ({meta.run_total_tokens} tok)")

    print(f"\n  Total tokens across 10 phrasings: {total_tokens}")
    print(f"\n  Known alias map has {len(_KEY_ALIASES)} entries.")
    print("  (Aliases actually used are logged at INFO level in the container logs.)")
    return True, total_tokens


def main():
    if settings.offline_demo:
        print("ERROR: OFFLINE_DEMO is enabled. Set OFFLINE_DEMO=0 to run live.")
        sys.exit(1)

    results = []
    grand_tokens = 0

    ok, tok = check_repair_loop()
    results.append(("Repair loop", ok)); grand_tokens += tok

    ok, tok = check_accounting()
    results.append(("Accounting", ok)); grand_tokens += tok

    ok, tok = check_phrasing_batch()
    results.append(("10-phrasing batch", ok)); grand_tokens += tok

    hr("Summary")
    for name, ok in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    print(f"\n  Grand total tokens: {grand_tokens}")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
