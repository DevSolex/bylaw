"""
app/policy.py — plain-text policy → validated Policy model.

parse_policy(text, client, call_counter, max_calls, meta) → Policy

The user's policy text is treated as DATA only. It is embedded inside a
clearly delimited block in the user message so the model cannot mistake it
for instructions. The system message (from prompts/policy_parse.md) tells
the model to extract structured JSON from it.

JSON repair: if the first response is not valid JSON, or contains unknown
keys after normalisation, we do one repair retry. If that also fails we
raise PolicyParseError.

Every SERV call is counted in call_counter and, if meta is supplied,
accumulated into the Run's ModelMeta totals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.models import Policy
from app.serv_client import ServClient, ServResponse

if TYPE_CHECKING:
    from app.models import ModelMeta

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "policy_parse.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")

# Canonical field names accepted by Policy
_CANONICAL_KEYS = frozenset(Policy.model_fields.keys())


class PolicyParseError(Exception):
    """Raised when policy text cannot be parsed into a valid Policy."""


class CallLimitError(Exception):
    """Raised when the SERV call cap for a Run would be exceeded."""


# ---------------------------------------------------------------------------
# Key-alias normalisation (fallback only — canonical names preferred)
# ---------------------------------------------------------------------------

_KEY_ALIASES: dict[str, str] = {
    "max_vault_allocation": "max_per_vault",
    "max_allocation_per_vault": "max_per_vault",
    "max_single_vault": "max_per_vault",
    "max_per_vault_allocation": "max_per_vault",
    "max_weight_per_vault": "max_per_vault",
    "min_liquidity": "min_liquid",
    "min_liquid_fraction": "min_liquid",
    "min_liquid_ratio": "min_liquid",
    "liquidity_floor": "min_liquid",
    "min_redeemable": "min_liquid",
    "max_redemption_time": "liquid_days",
    "max_redemption_time_days": "liquid_days",
    "liquidity_window": "liquid_days",
    "liquid_window_days": "liquid_days",
    "redemption_window": "liquid_days",
    "liquid_within_days": "liquid_days",
    "max_average_risk": "max_avg_risk",
    "max_risk": "max_avg_risk",
    "average_risk_ceiling": "max_avg_risk",
    "risk_ceiling": "max_avg_risk",
    "max_risk_score": "max_avg_risk",
    "min_average_apy": "min_avg_apy",
    "min_apy": "min_avg_apy",
    "minimum_apy": "min_avg_apy",
    "min_yield": "min_avg_apy",
    "minimum_average_apy": "min_avg_apy",
    "max_lockup_days": "max_redemption_days",
    "max_lockup": "max_redemption_days",
    "max_delay_days": "max_redemption_days",
}


def _normalise_policy_keys(data: dict) -> tuple[dict, list[str], list[str]]:
    """
    Normalise keys to canonical Policy field names.

    Returns
    -------
    (normalised_dict, aliases_used, unknown_keys)
    """
    result: dict = {}
    aliases_used: list[str] = []
    unknown_keys: list[str] = []

    for k, v in data.items():
        if k in _CANONICAL_KEYS:
            result[k] = v
        elif k in _KEY_ALIASES:
            canonical = _KEY_ALIASES[k]
            result[canonical] = v
            aliases_used.append(f"{k} → {canonical}")
            logger.info("policy parse: alias used: %s → %s", k, canonical)
        else:
            unknown_keys.append(k)
            logger.warning("policy parse: unknown key ignored: %r (value=%r)", k, v)

    return result, aliases_used, unknown_keys


def _load_and_normalise(text: str) -> tuple[dict, list[str], list[str]]:
    """Parse JSON text and normalise keys. Returns (data, aliases_used, unknown_keys)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        stripped = inner.strip()
    data = json.loads(stripped)
    return _normalise_policy_keys(data)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_policy(
    text: str,
    client: ServClient,
    call_counter: list[int],
    max_calls: int,
    meta: "ModelMeta | None" = None,
) -> Policy:
    """
    Parse free-form policy text into a validated Policy.

    Parameters
    ----------
    text        : User's plain-language policy (treated as data only).
    client      : ServClient instance (may be offline stub).
    call_counter: [int] — incremented for every SERV call.
    max_calls   : Hard cap.
    meta        : Optional ModelMeta to accumulate token totals into.

    Returns
    -------
    Policy with validated fields; missing fields use defaults (no constraint).
    """
    user_msg = (
        "Extract the investment-policy constraints from the text below.\n\n"
        "--- POLICY TEXT BEGIN ---\n"
        f"{text}\n"
        "--- POLICY TEXT END ---\n\n"
        "Output only the JSON object as specified."
    )

    raw_json: dict | None = None
    all_aliases: list[str] = []
    last_error: str = ""
    last_raw: str = ""

    for attempt in range(2):  # 0 = first try; 1 = repair retry
        _check_and_increment(call_counter, max_calls, "policy parse")

        if attempt == 0:
            messages = [{"role": "user", "content": user_msg}]
        else:
            messages = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": last_raw},
                {
                    "role": "user",
                    "content": (
                        f"That response had issues: {last_error}\n"
                        "Please output only the corrected JSON object using "
                        "EXACTLY the key names listed in the schema."
                    ),
                },
            ]

        resp: ServResponse = client.chat(
            messages=messages,
            system=_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )
        _log_and_update(resp, call_counter[0], meta)
        last_raw = resp.content

        try:
            normalised, aliases, unknown = _load_and_normalise(resp.content)
            all_aliases.extend(aliases)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = f"invalid JSON: {exc}"
            logger.warning("policy parse attempt %d: %s", attempt + 1, last_error)
            if attempt == 1:
                raise PolicyParseError(
                    f"Policy parsing failed after repair retry. "
                    f"Last output: {resp.content!r}. Error: {last_error}"
                ) from exc
            continue

        if unknown and attempt == 0:
            # Unknown keys after normalisation → repair retry
            last_error = (
                f"response contained unrecognised keys: {unknown}. "
                f"Use only these key names: {sorted(_CANONICAL_KEYS)}"
            )
            logger.warning("policy parse attempt %d: %s", attempt + 1, last_error)
            continue

        # Drop the unknown keys silently on the repair attempt and proceed
        raw_json = normalised
        break

    assert raw_json is not None

    try:
        policy = Policy.model_validate(raw_json)
    except ValidationError as exc:
        raise PolicyParseError(
            f"Policy JSON passed normalisation but failed pydantic validation: {exc}\n"
            f"Raw: {raw_json}"
        ) from exc

    # ── Sanity check: liquid_days vs max_redemption_days confusion ─────
    # If both are set and max_redemption_days <= liquid_days, the model
    # likely confused "redeemable within N days" (a floor) with a vault
    # exclusion. Strip max_redemption_days and warn.
    if (
        policy.max_redemption_days is not None
        and policy.liquid_days > 0
        and policy.max_redemption_days <= policy.liquid_days
    ):
        logger.warning(
            "policy parse: max_redemption_days=%d <= liquid_days=%d — "
            "likely a misparse of 'redeemable within N days'. "
            "Clearing max_redemption_days.",
            policy.max_redemption_days, policy.liquid_days,
        )
        policy = policy.model_copy(update={"max_redemption_days": None})

    if all_aliases:
        logger.info("policy parse: aliases used this run: %s", all_aliases)

    logger.info("policy parsed: %s", policy.model_dump(exclude_defaults=True))
    return policy


# ---------------------------------------------------------------------------
# Helpers shared with proposal.py
# ---------------------------------------------------------------------------

def _check_and_increment(counter: list[int], max_calls: int, context: str) -> None:
    if counter[0] >= max_calls:
        raise CallLimitError(
            f"SERV call limit of {max_calls} per run reached at '{context}'. "
            f"Increase SERV_MAX_CALLS_PER_RUN to allow more calls."
        )
    counter[0] += 1


def _log_and_update(resp: ServResponse, call_number: int, meta: "ModelMeta | None") -> None:
    """Log token usage and accumulate into meta if provided."""
    logger.info(
        "SERV call #%d  model=%s  latency=%.0fms  "
        "prompt=%s completion=%s total=%s",
        call_number,
        resp.model,
        resp.latency_ms,
        resp.prompt_tokens,
        resp.completion_tokens,
        resp.total_tokens,
    )
    if meta is None:
        return
    meta.total_calls += 1
    meta.latency_ms = resp.latency_ms
    meta.model = resp.model
    if resp.prompt_tokens is not None:
        meta.run_prompt_tokens += resp.prompt_tokens
        meta.prompt_tokens = resp.prompt_tokens
    if resp.completion_tokens is not None:
        meta.run_completion_tokens += resp.completion_tokens
        meta.completion_tokens = resp.completion_tokens
    if resp.total_tokens is not None:
        meta.run_total_tokens += resp.total_tokens
        meta.total_tokens = resp.total_tokens


# Keep old name for backward compat with any callers that imported it
_log_tokens = _log_and_update
