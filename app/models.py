"""
app/models.py — Pydantic v2 data models for Bylaw.

Every model used across the pipeline is defined here to keep imports clean
and avoid circular dependencies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Source literal — every vault field carries one of these
# ---------------------------------------------------------------------------

SourceLabel = Literal["live", "curated", "simulated"]

# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

class Vault(BaseModel):
    id: str
    name: str
    chain: str
    asset: str  # e.g. "USDC"

    apy: float | None = None                        # fraction; 0.07 == 7%
    apy_label: str | None = None                    # e.g. "indicative: underlying ETF yield, not realized"
    redemption_days: int | None = None              # 0 means instant
    settlement: Literal["sync", "async"] | None = None
    risk: int = Field(..., ge=1, le=5)              # 1 lowest, 5 highest
    paused: bool | None = None
    available_liquidity_usdc: float | None = None

    # One entry per field above; keys match field names
    data_sources: dict[str, SourceLabel] = Field(default_factory=dict)
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Policy — all optional; missing means "no constraint"
# ---------------------------------------------------------------------------

class Policy(BaseModel):
    max_per_vault: float = Field(default=1.0, ge=0.0, le=1.0)
    min_liquid: float = Field(default=0.0, ge=0.0, le=1.0)
    liquid_days: int = Field(default=0, ge=0)
    max_avg_risk: float = Field(default=5.0, ge=1.0, le=5.0)
    min_avg_apy: float | None = None
    max_redemption_days: int | None = None
    excluded_vault_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Holdings — map of vault_id -> amount in USDC
# ---------------------------------------------------------------------------

Holdings = dict[str, float]  # vault_id -> amount_usdc


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

class Proposal(BaseModel):
    allocation: dict[str, float]  # vault_id -> weight (0-1, sum == 1)
    rationale: str


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class Trade(BaseModel):
    vault_id: str
    action: Literal["deposit", "redeem"]
    amount_usdc: float
    settlement: Literal["sync", "async"] | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# VerifierResult — returned by verifier.verify()
# ---------------------------------------------------------------------------

class RuleResult(BaseModel):
    rule: str
    passed: bool
    detail: str = ""


class VerifierResult(BaseModel):
    passed: bool
    problems: list[str]           # human-readable failures only
    checklist: list[RuleResult]   # per-rule pass/fail for the UI


# ---------------------------------------------------------------------------
# FeasibilityResult — returned by verifier.check_feasibility()
# ---------------------------------------------------------------------------

class FeasibilityResult(BaseModel):
    feasible: bool
    reason: str = ""   # populated only when infeasible


# ---------------------------------------------------------------------------
# Attempt — one model call + verifier verdict inside a Run
# ---------------------------------------------------------------------------

class Attempt(BaseModel):
    proposal: Proposal
    result: VerifierResult


# ---------------------------------------------------------------------------
# RunInput — what the user submitted
# ---------------------------------------------------------------------------

class RunInput(BaseModel):
    policy_text: str
    amount_usdc: float
    holdings: Holdings = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# RunFinal — the accepted or rejected outcome
# ---------------------------------------------------------------------------

class RunFinal(BaseModel):
    allocation: dict[str, float] | None = None
    rationale: str = ""
    verified: bool
    trades: list[Trade] = Field(default_factory=list)
    reason: str = ""   # populated when verified=False or infeasible


# ---------------------------------------------------------------------------
# ModelMeta — SERV call metadata (one entry per call, aggregated on Run)
# ---------------------------------------------------------------------------

class StepMeta(BaseModel):
    """Token usage for one logical step within a Run."""
    step: str          # "policy_parse", "policy_repair", "proposal_1", "proposal_2", ...
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelMeta(BaseModel):
    model_config = {"protected_namespaces": ()}

    model: str
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None         # prompt + completion

    # Running totals across all SERV calls in the Run
    total_calls: int = 0
    run_prompt_tokens: int = 0
    run_completion_tokens: int = 0
    run_total_tokens: int = 0

    # Per-step breakdown (parse, repair, proposal attempts)
    steps: list[StepMeta] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run — the full audit record; saved as data/runs/<id>.json
# ---------------------------------------------------------------------------

class Run(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: str
    created_at: datetime

    input: RunInput
    policy: Policy
    vault_snapshot: list[Vault]

    attempts: list[Attempt] = Field(default_factory=list)
    final: RunFinal | None = None

    decision: Literal["pending", "approved", "rejected"] = "pending"
    execution: dict | None = None   # filled after approval

    model_meta: ModelMeta | None = None
