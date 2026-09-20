"""
tests/test_api.py — API endpoint tests.

Run with OFFLINE_DEMO=1 DATA_MODE=simulated (set in pytest.ini or via env).
No network, no real API key required.
"""
from __future__ import annotations

import os
# These must be set before app.config is imported
os.environ["OFFLINE_DEMO"] = "1"
os.environ["DATA_MODE"] = "simulated"
os.environ["SERV_API_KEY"] = "offline"

from unittest.mock import patch
from fastapi.testclient import TestClient

# Patch settings *before* app.main is imported by forcing re-evaluation
import app.config as _cfg
_cfg.settings.offline_demo = True
_cfg.settings.data_mode = "simulated"
_cfg.settings.serv_api_key = "offline"

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/vaults
# ---------------------------------------------------------------------------

def test_get_vaults_simulated():
    r = client.get("/api/vaults")
    assert r.status_code == 200
    d = r.json()
    assert d["data_mode"] == "simulated"
    assert len(d["vaults"]) >= 4
    assert "banner" in d


def test_vaults_have_source_labels():
    d = client.get("/api/vaults").json()
    for v in d["vaults"]:
        assert v["data_sources"], f"{v['id']} missing data_sources"


# ---------------------------------------------------------------------------
# /api/policy/parse
# ---------------------------------------------------------------------------

def test_parse_policy_offline():
    r = client.post("/api/policy/parse", json={"text": "max 40% per vault"})
    assert r.status_code == 200
    d = r.json()
    assert "policy" in d
    assert d["offline_demo"] is True


def test_parse_policy_empty_text():
    r = client.post("/api/policy/parse", json={"text": ""})
    assert r.status_code == 200
    assert "policy" in r.json()


# ---------------------------------------------------------------------------
# /api/propose — offline stub (no real SERV call)
# ---------------------------------------------------------------------------

def test_propose_offline_returns_run():
    r = client.post("/api/propose", json={
        "policy_text": "No more than 50% per vault. Average risk 3 or lower.",
        "amount_usdc": 100000,
    })
    assert r.status_code == 200
    d = r.json()
    assert "run_id" in d
    assert "verified" in d
    assert d["offline_demo"] is True
    assert d["data_mode"] == "simulated"


def test_propose_verified_or_reason():
    r = client.post("/api/propose", json={
        "policy_text": "max 40% per vault",
        "amount_usdc": 50000,
    })
    d = r.json()
    # Either verified with allocation, or unverified with reason
    if d["verified"]:
        assert d["allocation"] is not None
    else:
        assert d["reason"]


def test_propose_with_explicit_policy():
    r = client.post("/api/propose", json={
        "policy": {"max_per_vault": 0.5, "max_avg_risk": 3.0},
        "amount_usdc": 100000,
    })
    assert r.status_code == 200


def test_propose_no_policy_returns_422():
    r = client.post("/api/propose", json={"amount_usdc": 100000})
    assert r.status_code == 422


def test_propose_returns_token_counts():
    r = client.post("/api/propose", json={
        "policy_text": "max 40% per vault",
        "amount_usdc": 100000,
    })
    d = r.json()
    assert "tokens" in d
    assert "serv_calls" in d


# ---------------------------------------------------------------------------
# /api/runs/{id}/decision
# ---------------------------------------------------------------------------

def _make_run() -> str:
    r = client.post("/api/propose", json={
        "policy_text": "max 40% per vault",
        "amount_usdc": 100000,
    })
    return r.json()["run_id"]


def test_approve_run():
    run_id = _make_run()
    run = client.get(f"/api/runs/{run_id}").json()
    if not run.get("final", {}).get("verified"):
        return
    r2 = client.post(f"/api/runs/{run_id}/decision", json={"decision": "approve"})
    assert r2.status_code == 200
    assert r2.json()["decision"] == "approved"


def test_reject_run():
    run_id = _make_run()
    run = client.get(f"/api/runs/{run_id}").json()
    if not run.get("final", {}).get("verified"):
        return
    r2 = client.post(f"/api/runs/{run_id}/decision", json={"decision": "reject"})
    assert r2.status_code == 200
    assert r2.json()["decision"] == "rejected"


def test_decision_invalid_choice():
    run_id = _make_run()
    r = client.post(f"/api/runs/{run_id}/decision", json={"decision": "maybe"})
    assert r.status_code == 422


def test_decision_not_found():
    r = client.post("/api/runs/doesnotexist/decision", json={"decision": "approve"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------

def test_list_runs():
    # Ensure at least one run exists
    client.post("/api/propose", json={"policy_text": "test", "amount_usdc": 1000})
    r = client.get("/api/runs")
    assert r.status_code == 200
    d = r.json()
    assert "runs" in d
    assert isinstance(d["runs"], list)


def test_get_run_not_found():
    r = client.get("/api/runs/nonexistent")
    assert r.status_code == 404
