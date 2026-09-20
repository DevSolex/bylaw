"""
app/policy.py — plain-text policy → validated Policy model.

parse_policy(text, client, call_counter) → Policy

The user's policy text is treated as DATA only. It is embedded inside a
clearly delimited block in the user message so the model cannot mistake it
for instructions. The system message (from prompts/policy_parse.md) tells
the model to extract structured JSON from it.

JSON repair: if the first response is not valid JSON we do one retry,
sending the parse error back. If that also fails we raise PolicyParseError.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import ValidationError

from app.models import Policy
from app.serv_client import ServClient, ServResponse

logger = logging.getLogger(__name__)

# Load system prompt once at import time
_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "policy_parse.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


class PolicyParseError(Exception):
    """Raised when policy text cannot be parsed into a valid Policy."""


class CallLimitError(Exception):
    """Raised when the SERV call cap for a Run would be exceeded."""


def _load_json(text: str) -> dict:
    """
    Extract a JSON object from the model reply.
    Handles both bare JSON and code-fenced JSON (```...```).
    Also normalises key aliases the model sometimes uses.
    """
    stripped = text.strip()
    # Strip optional ``` fences
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```json or ```) and last (```)
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        stripped = inner.strip()
    data = json.loads(stripped)
    return _normalise_policy_keys(data)


# Common aliases the model uses for policy keys
_KEY_ALIASES: dict[str, str] = {
    # max_per_vault aliases
    "max_vault_allocation": "max_per_vault",
    "max_allocation_per_vault": "max_per_vault",
    "max_single_vault": "max_per_vault",
    "max_per_vault_allocation": "max_per_vault",
    "max_weight_per_vault": "max_per_vault",
    # min_liquid aliases
    "min_liquidity": "min_liquid",
    "min_liquid_fraction": "min_liquid",
    "min_liquid_ratio": "min_liquid",
    "liquidity_floor": "min_liquid",
    "min_redeemable": "min_liquid",
    # liquid_days aliases
    "max_redemption_time": "liquid_days",
    "max_redemption_time_days": "liquid_days",
    "liquidity_window": "liquid_days",
    "liquid_window_days": "liquid_days",
    "redemption_window": "liquid_days",
    "liquid_within_days": "liquid_days",
    # max_avg_risk aliases
    "max_average_risk": "max_avg_risk",
    "max_risk": "max_avg_risk",
    "average_risk_ceiling": "max_avg_risk",
    "risk_ceiling": "max_avg_risk",
    "max_risk_score": "max_avg_risk",
    # min_avg_apy aliases
    "min_average_apy": "min_avg_apy",
    "min_apy": "min_avg_apy",
    "minimum_apy": "min_avg_apy",
    "min_yield": "min_avg_apy",
    "minimum_average_apy": "min_avg_apy",
    # max_redemption_days aliases
    "max_lockup_days": "max_redemption_days",
    "max_lockup": "max_redemption_days",
    "max_delay_days": "max_redemption_days",
}


def _normalise_policy_keys(data: dict) -> dict:
    """Replace known alias keys with the canonical Policy field names."""
    result = {}
    for k, v in data.items():
        canonical = _KEY_ALIASES.get(k, k)
        result[canonical] = v
    return result


def parse_policy(
    text: str,
    client: ServClient,
    call_counter: list[int],
    max_calls: int,
) -> Policy:
    """
    Parse free-form policy text into a validated Policy.

    Parameters
    ----------
    text:
        The user's plain-language policy. Treated as data, not instructions.
    client:
        ServClient instance (may be offline stub).
    call_counter:
        Mutable single-element list tracking total SERV calls this Run.
        Incremented here; checked against max_calls before each call.
    max_calls:
        Hard cap on total SERV calls for this Run.

    Returns
    -------
    Policy
        Validated policy. Missing fields use Policy defaults (no constraint).

    Raises
    ------
    CallLimitError:
        If the call cap would be exceeded.
    PolicyParseError:
        If the model returns invalid JSON after one repair retry, or if
        pydantic validation fails with un-repairable data.
    """
    # ── User message — policy text is DATA, delimited clearly ──────────
    user_msg = (
        "Extract the investment-policy constraints from the text below.\n\n"
        "--- POLICY TEXT BEGIN ---\n"
        f"{text}\n"
        "--- POLICY TEXT END ---\n\n"
        "Output only the JSON object as specified."
    )

    raw_json: dict | None = None
    last_error: str = ""

    for attempt in range(2):  # attempt 0 = first try; attempt 1 = repair retry
        _check_and_increment(call_counter, max_calls, context="policy parse")

        if attempt == 0:
            messages = [{"role": "user", "content": user_msg}]
        else:
            # Repair retry: send the parse error back
            messages = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": last_raw},  # type: ignore[name-defined]
                {
                    "role": "user",
                    "content": (
                        f"That response was not valid JSON. Error: {last_error}\n"
                        "Please output only the corrected JSON object."
                    ),
                },
            ]

        resp: ServResponse = client.chat(
            messages=messages,
            system=_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )
        _log_tokens(resp, call_counter[0])
        last_raw = resp.content  # noqa: F841  (used in repair branch above)

        try:
            raw_json = _load_json(resp.content)
            break
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning("policy parse: invalid JSON on attempt %d: %s", attempt + 1, exc)
            if attempt == 1:
                raise PolicyParseError(
                    f"Policy parsing failed after repair retry. "
                    f"Last model output: {resp.content!r}. "
                    f"Parse error: {last_error}"
                ) from exc

    assert raw_json is not None

    # Drop unknown keys, validate with pydantic
    try:
        policy = Policy.model_validate(raw_json)
    except ValidationError as exc:
        raise PolicyParseError(
            f"Policy JSON parsed but failed validation: {exc}\n"
            f"Raw JSON: {raw_json}"
        ) from exc

    logger.info("policy parsed: %s", policy.model_dump(exclude_defaults=True))
    return policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_and_increment(counter: list[int], max_calls: int, context: str) -> None:
    if counter[0] >= max_calls:
        raise CallLimitError(
            f"SERV call limit of {max_calls} per run reached at '{context}'. "
            f"Increase SERV_MAX_CALLS_PER_RUN to allow more calls."
        )
    counter[0] += 1


def _log_tokens(resp: ServResponse, call_number: int) -> None:
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
