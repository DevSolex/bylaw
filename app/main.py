"""
Bylaw — FastAPI application.
"""
from __future__ import annotations

import logging
import os
import traceback
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.execution import compute_trades, dry_run_report
from app.models import (
    Holdings, ModelMeta, Policy, Run, RunFinal, RunInput,
)
from app.policy import parse_policy, CallLimitError, PolicyParseError
from app.proposal import propose
from app.runs import list_runs, load_run, new_run_id, save_run, run_status
from app.serv_client import ServClient
from app.vaults import get_vaults
from app.vaults.ixs import capacity_warnings

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Bylaw",
    description="Policy-driven allocator for tokenized RWA yield vaults.",
    version="0.4.0",
)


# ---------------------------------------------------------------------------
# Global exception handler — always returns JSON, never plain-text 500
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = uuid.uuid4().hex[:8]
    logger.error("Unhandled exception [req=%s] %s: %s",
                 request_id, type(exc).__name__, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": f"{type(exc).__name__}: {exc}",
            "request_id": request_id,
        },
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "request_error", "detail": exc.detail},
    )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ParseRequest(BaseModel):
    text: str


class ProposeRequest(BaseModel):
    policy_text: str | None = None
    policy: dict | None = None          # pre-parsed Policy fields
    amount_usdc: float = 100_000.0
    holdings: Holdings = {}


class DecisionRequest(BaseModel):
    decision: str                       # "approve" | "reject"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Vault snapshot
# ---------------------------------------------------------------------------

@app.get("/api/vaults", tags=["data"])
def api_vaults():
    vaults = get_vaults(settings.data_mode)
    live_count      = sum(1 for v in vaults if any(s == "live"      for s in v.data_sources.values()))
    curated_count   = sum(1 for v in vaults if any(s == "curated"   for s in v.data_sources.values()) and not any(s == "live" for s in v.data_sources.values()))
    simulated_count = sum(1 for v in vaults if all(s in ("simulated","curated") for s in v.data_sources.values()) and not any(s == "live" for s in v.data_sources.values()))

    return {
        "data_mode": settings.data_mode,
        "offline_demo": settings.offline_demo,
        "vault_count": len(vaults),
        "live_count": live_count,
        "simulated_count": simulated_count,
        "vaults": [v.model_dump(mode="json") for v in vaults],
        "banner": _build_banner(vaults),
    }


def _build_banner(vaults) -> str:
    if settings.offline_demo:
        return "⚠ OFFLINE DEMO: model output is stubbed. No real API calls."
    sources = set()
    for v in vaults:
        sources.update(v.data_sources.values())
    parts = []
    if "live" in sources:
        parts.append("Live IXS data")
    if "simulated" in sources:
        parts.append("Simulated peers")
    if "curated" in sources and "live" not in sources:
        parts.append("Curated data (live unavailable)")
    return " · ".join(parts) if parts else "Data loaded"


# ---------------------------------------------------------------------------
# Policy parsing
# ---------------------------------------------------------------------------

@app.post("/api/policy/parse", tags=["pipeline"])
def api_parse_policy(req: ParseRequest):
    client = ServClient()
    counter = [0]
    meta = ModelMeta(model=client.model, total_calls=0,
                     run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)
    try:
        policy = parse_policy(req.text, client, counter, settings.serv_max_calls_per_run, meta)
    except (PolicyParseError, CallLimitError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    captured = policy.model_dump(exclude_defaults=True)
    return {
        "policy": policy.model_dump(),
        "captured_constraints": captured,
        "serv_calls": meta.total_calls,
        "tokens_used": meta.run_total_tokens,
        "offline_demo": settings.offline_demo,
    }


# ---------------------------------------------------------------------------
# Propose — full pipeline (parse + propose + verify)
# ---------------------------------------------------------------------------

@app.post("/api/propose", tags=["pipeline"])
def api_propose(req: ProposeRequest):
    try:
        return _do_propose(req)
    except HTTPException:
        raise
    except Exception as exc:
        request_id = uuid.uuid4().hex[:8]
        logger.error("propose error [req=%s]: %s", request_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Proposal failed: {type(exc).__name__}: {exc} [req={request_id}]",
        )


def _do_propose(req: ProposeRequest):
    # Resolve policy
    if req.policy_text:
        client = ServClient()
        counter = [0]
        meta = ModelMeta(model=client.model, total_calls=0,
                         run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)
        try:
            policy = parse_policy(
                req.policy_text, client, counter,
                settings.serv_max_calls_per_run, meta
            )
        except (PolicyParseError, CallLimitError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    elif req.policy:
        try:
            policy = Policy.model_validate(req.policy)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid policy: {exc}")
        client = ServClient()
        counter = [0]
        meta = ModelMeta(model=client.model, total_calls=0,
                         run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)
    else:
        raise HTTPException(status_code=422, detail="Provide policy_text or policy")

    vaults = get_vaults(settings.data_mode)
    final, attempts, meta = propose(
        policy, vaults, req.amount_usdc, req.holdings,
        client, counter, settings.serv_max_calls_per_run, meta,
    )

    # Capacity warnings
    cap_warns: list[dict] = []
    if final.verified and final.allocation:
        cap_warns = capacity_warnings(
            final.allocation, req.amount_usdc, vaults,
            warn_share=settings.capacity_warn_share,
        )

    # Build run record
    run = Run(
        id=new_run_id(),
        created_at=datetime.now(timezone.utc),
        input=RunInput(
            policy_text=req.policy_text or "",
            amount_usdc=req.amount_usdc,
            holdings=req.holdings,
        ),
        policy=policy,
        vault_snapshot=vaults,
        attempts=attempts,
        final=final,
        decision="pending",
        model_meta=meta,
    )
    save_run(run)

    return {
        "run_id": run.id,
        "verified": final.verified,
        "allocation": final.allocation,
        "rationale": final.rationale,
        "reason": final.reason,
        "attempts": len(attempts),
        "verifier_checklist": [
            a.result.model_dump() for a in attempts
        ],
        "capacity_warnings": cap_warns,
        "policy": policy.model_dump(),
        "captured_constraints": policy.model_dump(exclude_defaults=True),
        "serv_calls": meta.total_calls,
        "tokens": {
            "prompt": meta.run_prompt_tokens,
            "completion": meta.run_completion_tokens,
            "total": meta.run_total_tokens,
        },
        "steps": [s.model_dump() for s in meta.steps],
        "data_mode": settings.data_mode,
        "offline_demo": settings.offline_demo,
        "banner": _build_banner(vaults),
    }


# ---------------------------------------------------------------------------
# Decision: approve / reject
# ---------------------------------------------------------------------------

@app.post("/api/runs/{run_id}/decision", tags=["pipeline"])
def api_decision(run_id: str, req: DecisionRequest):
    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'")

    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.decision != "pending":
        raise HTTPException(status_code=409, detail=f"Run already {run.decision}")
    if not run.final or not run.final.verified:
        raise HTTPException(status_code=422, detail="Cannot approve an unverified run")

    run.decision = "approved" if req.decision == "approve" else "rejected"  # type: ignore[assignment]

    if req.decision == "approve":
        vaults = run.vault_snapshot
        trades = compute_trades(
            run.final.allocation or {},
            run.input.amount_usdc,
            run.input.holdings,
            vaults,
        )
        run.final.trades = trades
        run.execution = dry_run_report(
            trades, vaults, run.input.amount_usdc, run.final.allocation or {}
        )

    save_run(run)

    return {
        "run_id": run.id,
        "decision": run.decision,
        "execution": run.execution,
        "trades": [t.model_dump() for t in (run.final.trades if run.final else [])],
    }


# ---------------------------------------------------------------------------
# Manual adjustment — re-verify without calling the model
# ---------------------------------------------------------------------------

class AdjustRequest(BaseModel):
    allocation: dict[str, float]   # vault_id → weight, must sum to 1


@app.post("/api/runs/{run_id}/adjust", tags=["pipeline"])
def api_adjust(run_id: str, req: AdjustRequest):
    """
    Re-verify a manually adjusted allocation without calling the model.
    Accepts a new weight dict, runs the verifier, saves result back to the run.
    The model is not involved — this is a pure deterministic check.
    """
    from app.models import Proposal, RunFinal
    from app.verifier import verify

    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.decision != "pending":
        raise HTTPException(status_code=409, detail=f"Run already {run.decision}")

    proposal = Proposal(allocation=req.allocation, rationale="[manually adjusted]")
    result = verify(proposal, run.policy, run.vault_snapshot, run.input.amount_usdc)

    cap_warns = capacity_warnings(
        req.allocation, run.input.amount_usdc, run.vault_snapshot,
        warn_share=settings.capacity_warn_share,
    )

    if result.passed:
        run.final = RunFinal(
            allocation=req.allocation,
            rationale="[manually adjusted — not model-generated]",
            verified=True,
        )
    else:
        run.final = RunFinal(
            verified=False,
            reason="; ".join(result.problems),
        )
    save_run(run)

    return {
        "run_id": run.id,
        "verified": result.passed,
        "allocation": req.allocation if result.passed else None,
        "problems": result.problems,
        "checklist": result.model_dump()["checklist"],
        "capacity_warnings": cap_warns,
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@app.get("/api/runs", tags=["audit"])
def api_list_runs():
    runs = list_runs(limit=200)
    return {
        "count": len(runs),
        "runs": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "status": run_status(r),
                "decision": r.decision,
                "verified": r.final.verified if r.final else None,
                "amount_usdc": r.input.amount_usdc,
                "serv_calls": r.model_meta.total_calls if r.model_meta else 0,
                "tokens": r.model_meta.run_total_tokens if r.model_meta else 0,
            }
            for r in runs
        ],
    }


@app.get("/api/runs/{run_id}", tags=["audit"])
def api_get_run(run_id: str):
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
