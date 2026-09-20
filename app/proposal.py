"""
app/proposal.py — propose an allocation and verify it, with retry.

propose(policy, vaults, amount_usdc, holdings, client, call_counter, max_calls)
    → (RunFinal, list[Attempt], ModelMeta)

Flow (spec §5.2):
  1. Check feasibility first (exact LP + grid). If infeasible, skip model.
  2. Ask SERV for a JSON allocation (up to 3 attempts).
  3. After each attempt run the verifier; failures feed back as problems.
  4. All 3 fail → RunFinal(verified=False).

All SERV calls — including the policy-parse call done before propose() is
called — share the same call_counter and ModelMeta so the Run total is exact.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.models import (
    Attempt,
    FeasibilityResult,
    Holdings,
    ModelMeta,
    Policy,
    Proposal,
    RunFinal,
    Vault,
    VerifierResult,
)
from app.policy import CallLimitError, _check_and_increment, _log_and_update
from app.serv_client import ServClient, ServResponse
from app.verifier import check_feasibility, verify

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "proposal.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")

_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _vault_summary(vaults: list[Vault]) -> str:
    rows = []
    for v in vaults:
        rows.append({
            "id": v.id,
            "name": v.name,
            "apy": v.apy,
            "redemption_days": v.redemption_days,
            "risk": v.risk,
            "paused": v.paused,
            "available_liquidity_usdc": v.available_liquidity_usdc,
        })
    return json.dumps(rows, indent=2)


def _policy_summary(policy: Policy) -> str:
    return json.dumps(policy.model_dump(exclude_defaults=False), indent=2)


def _holdings_summary(holdings: Holdings) -> str:
    if not holdings:
        return "none (starting from cash)"
    return json.dumps(holdings, indent=2)


def _build_user_message(
    policy: Policy,
    vaults: list[Vault],
    amount_usdc: float,
    holdings: Holdings,
    previous_problems: list[str],
) -> str:
    parts = [
        "Propose an allocation that satisfies the policy below.\n",
        "--- POLICY BEGIN ---",
        _policy_summary(policy),
        "--- POLICY END ---\n",
        "--- VAULT SNAPSHOT BEGIN ---",
        _vault_summary(vaults),
        "--- VAULT SNAPSHOT END ---\n",
        f"Total amount to allocate: {amount_usdc:,.2f} USDC",
        f"Current holdings: {_holdings_summary(holdings)}",
    ]
    if previous_problems:
        parts += [
            "\nThe previous allocation failed verification for these reasons:",
            *[f"  - {p}" for p in previous_problems],
            "\nPlease correct ALL of these issues in your new allocation.",
        ]
    parts.append("\nOutput only the JSON object as specified.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Proposal parser (normalises alternate model output shapes)
# ---------------------------------------------------------------------------

def _parse_proposal(raw: str) -> Proposal:
    """
    Parse model reply into a Proposal.

    Handles bare JSON and ```-fenced JSON.
    Normalises list-of-dicts shapes:
      {"allocations": [{"vault_id": "x", "weight": 0.5}], ...}
      {"allocation": [{"vault_id": "x", "weight": 0.5}], ...}
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        stripped = inner.strip()
    data = json.loads(stripped)

    alloc = data.get("allocation") or data.get("allocations")
    if isinstance(alloc, list):
        normalised: dict[str, float] = {}
        for item in alloc:
            vid = item.get("vault_id") or item.get("id")
            weight = item.get("weight") or item.get("amount_usdc") or 0.0
            if vid:
                normalised[str(vid)] = float(weight)
        data["allocation"] = normalised
        data.pop("allocations", None)

    return Proposal.model_validate(data)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def propose(
    policy: Policy,
    vaults: list[Vault],
    amount_usdc: float,
    holdings: Holdings,
    client: ServClient,
    call_counter: list[int],
    max_calls: int,
    meta: ModelMeta | None = None,
) -> tuple[RunFinal, list[Attempt], ModelMeta]:
    """
    Run the full propose-verify loop.

    call_counter and meta are shared with parse_policy so the Run's SERV
    call count and token total include the policy-parse call(s).

    Returns (RunFinal, attempts, meta).
    RunFinal.verified is True only if a proposal passed the verifier.
    """
    if meta is None:
        meta = ModelMeta(
            model=client.model,
            total_calls=0,
            run_prompt_tokens=0,
            run_completion_tokens=0,
            run_total_tokens=0,
        )

    attempts: list[Attempt] = []

    # ── 1. Feasibility check ───────────────────────────────────────────
    feasibility: FeasibilityResult = check_feasibility(policy, vaults, amount_usdc)
    if not feasibility.feasible:
        logger.info("proposal: infeasible — %s", feasibility.reason)
        return RunFinal(verified=False, reason=feasibility.reason), attempts, meta

    # ── 2. Proposal loop ───────────────────────────────────────────────
    previous_problems: list[str] = []
    last_verifier_result: VerifierResult | None = None

    for attempt_num in range(1, _MAX_ATTEMPTS + 1):
        logger.info("proposal: attempt %d/%d", attempt_num, _MAX_ATTEMPTS)

        try:
            _check_and_increment(call_counter, max_calls, f"proposal attempt {attempt_num}")
        except CallLimitError:
            reason = (
                f"SERV call limit reached before attempt {attempt_num}. "
                f"Increase SERV_MAX_CALLS_PER_RUN."
            )
            return RunFinal(verified=False, reason=reason), attempts, meta

        user_msg = _build_user_message(
            policy, vaults, amount_usdc, holdings, previous_problems
        )

        resp: ServResponse = client.chat(
            messages=[{"role": "user", "content": user_msg}],
            system=_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
            _stub_vaults=vaults,
        )
        _log_and_update(resp, call_counter[0], meta)
        logger.info(
            "proposal: running totals  calls=%d prompt=%d completion=%d total=%d",
            meta.total_calls,
            meta.run_prompt_tokens,
            meta.run_completion_tokens,
            meta.run_total_tokens,
        )

        # Parse
        try:
            proposal = _parse_proposal(resp.content)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.warning("proposal attempt %d: parse error: %s", attempt_num, exc)
            previous_problems = [f"Your response was not valid JSON: {exc}"]
            continue

        # Verify
        result: VerifierResult = verify(proposal, policy, vaults, amount_usdc)
        attempts.append(Attempt(proposal=proposal, result=result))
        last_verifier_result = result

        if result.passed:
            logger.info("proposal: verified on attempt %d", attempt_num)
            return (
                RunFinal(
                    allocation=proposal.allocation,
                    rationale=proposal.rationale,
                    verified=True,
                ),
                attempts,
                meta,
            )

        logger.info(
            "proposal: attempt %d failed verification: %s",
            attempt_num, result.problems,
        )
        previous_problems = result.problems

    # All attempts exhausted
    last_problems = last_verifier_result.problems if last_verifier_result else previous_problems
    reason = (
        f"All {_MAX_ATTEMPTS} proposal attempts failed verification. "
        f"Last problems: {'; '.join(last_problems)}"
    )
    logger.warning("proposal: all attempts failed. %s", reason)
    return RunFinal(verified=False, reason=reason), attempts, meta
