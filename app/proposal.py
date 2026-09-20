"""
app/proposal.py — propose an allocation and verify it, with retry.

propose(policy, vaults, amount_usdc, holdings, client, call_counter, max_calls)
    → (RunFinal, list[Attempt], ModelMeta)

Flow (spec §5.2):
  1. Check feasibility first. If infeasible, skip the model entirely.
  2. Ask SERV for a JSON allocation (up to 3 attempts).
  3. After each attempt, run the verifier.
  4. If the verifier passes → return the allocation as verified.
  5. If the verifier fails → pass the problem list back in the next prompt.
  6. If all 3 attempts fail → return verified=False with the last problems.

All SERV calls go through ServClient which enforces the system message,
uses max_completion_tokens, and never sends temperature.

Policy text and vault data are embedded as DATA in delimited blocks —
never as instructions to the model.
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
from app.policy import CallLimitError, _check_and_increment, _log_tokens
from app.serv_client import ServClient, ServResponse
from app.verifier import check_feasibility, verify

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "proposal.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")

_MAX_ATTEMPTS = 3


def _vault_summary(vaults: list[Vault]) -> str:
    """Compact JSON representation of vaults — embedded as DATA in the prompt."""
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


def _parse_proposal(raw: str) -> Proposal:
    """
    Parse the model reply into a Proposal.
    Handles bare JSON and ```-fenced JSON.

    Also normalises two alternate shapes the model sometimes emits:
      {"allocations": [{"vault_id": "x", "weight": 0.5}, ...], "rationale": "..."}
      {"allocation": [{"vault_id": "x", "weight": 0.5}, ...], "rationale": "..."}
    Both are converted to {"allocation": {"x": 0.5, ...}, "rationale": "..."}

    Raises ValueError on parse failure, ValidationError on schema failure.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        stripped = inner.strip()
    data = json.loads(stripped)

    # Normalise list-of-dicts allocation shapes
    alloc = data.get("allocation") or data.get("allocations")
    if isinstance(alloc, list):
        # Each element: {"vault_id": "x", "weight": 0.5} or {"id": "x", "weight": 0.5}
        normalized: dict[str, float] = {}
        for item in alloc:
            vid = item.get("vault_id") or item.get("id")
            weight = item.get("weight") or item.get("amount_usdc") or 0.0
            if vid:
                normalized[str(vid)] = float(weight)
        data["allocation"] = normalized
        data.pop("allocations", None)

    return Proposal.model_validate(data)


def propose(
    policy: Policy,
    vaults: list[Vault],
    amount_usdc: float,
    holdings: Holdings,
    client: ServClient,
    call_counter: list[int],
    max_calls: int,
) -> tuple[RunFinal, list[Attempt], ModelMeta]:
    """
    Run the full propose-verify loop.

    Returns
    -------
    (RunFinal, attempts, model_meta)
        RunFinal.verified is True only if a proposal passed the verifier.
        attempts contains every model call and verifier verdict.
        model_meta contains per-run token totals.
    """
    attempts: list[Attempt] = []
    meta = ModelMeta(
        model=client.model,
        total_calls=0,
        run_prompt_tokens=0,
        run_completion_tokens=0,
        run_total_tokens=0,
    )

    def _update_meta(resp: ServResponse) -> None:
        meta.total_calls += 1
        meta.latency_ms = resp.latency_ms
        if resp.prompt_tokens is not None:
            meta.run_prompt_tokens += resp.prompt_tokens
            meta.prompt_tokens = resp.prompt_tokens
        if resp.completion_tokens is not None:
            meta.run_completion_tokens += resp.completion_tokens
            meta.completion_tokens = resp.completion_tokens
        if resp.total_tokens is not None:
            meta.run_total_tokens += resp.total_tokens
            meta.total_tokens = resp.total_tokens
        meta.model = resp.model

    # ── 1. Feasibility check — skip model if impossible ────────────────
    feasibility: FeasibilityResult = check_feasibility(policy, vaults, amount_usdc)
    if not feasibility.feasible:
        logger.info("proposal: infeasible — skipping model. reason: %s", feasibility.reason)
        return (
            RunFinal(verified=False, reason=feasibility.reason),
            attempts,
            meta,
        )

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
        )
        _update_meta(resp)
        _log_tokens(resp, call_counter[0])
        logger.info(
            "proposal: running token total  prompt=%d completion=%d total=%d",
            meta.run_prompt_tokens,
            meta.run_completion_tokens,
            meta.run_total_tokens,
        )

        # Parse the proposal
        try:
            proposal = _parse_proposal(resp.content)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.warning("proposal attempt %d: parse error: %s", attempt_num, exc)
            # Count as a failed attempt; pass error as a problem to retry
            previous_problems = [f"Your response was not valid JSON: {exc}"]
            # No Attempt recorded when parse completely fails
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
            attempt_num,
            result.problems,
        )
        previous_problems = result.problems

    # ── All attempts exhausted ─────────────────────────────────────────
    last_problems = last_verifier_result.problems if last_verifier_result else previous_problems
    reason = (
        f"All {_MAX_ATTEMPTS} proposal attempts failed verification. "
        f"Last problems: {'; '.join(last_problems)}"
    )
    logger.warning("proposal: all attempts failed. %s", reason)
    return RunFinal(verified=False, reason=reason), attempts, meta
