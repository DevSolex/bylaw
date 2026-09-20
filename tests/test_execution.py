"""
tests/test_execution.py — unit tests for compute_trades and dry_run_report.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.execution import compute_trades, dry_run_report
from app.models import Vault

_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

def v(id, settlement="sync", redemption_days=1):
    return Vault(id=id, name=id, chain="eth", asset="USDC",
                 risk=2, settlement=settlement, redemption_days=redemption_days,
                 fetched_at=_NOW)

VAULTS = [v("v1"), v("v2", "async", 30), v("v3")]


def test_deposit_from_cash():
    trades = compute_trades({"v1": 0.6, "v2": 0.4}, 100_000, {}, VAULTS)
    actions = {t.vault_id: t for t in trades}
    assert actions["v1"].action == "deposit"
    assert abs(actions["v1"].amount_usdc - 60_000) < 0.01
    assert actions["v2"].action == "deposit"
    assert abs(actions["v2"].amount_usdc - 40_000) < 0.01


def test_redeem_from_existing():
    holdings = {"v1": 100_000.0}
    trades = compute_trades({"v1": 0.0, "v2": 1.0}, 100_000, holdings, VAULTS)
    actions = {t.vault_id: t for t in trades}
    assert actions["v1"].action == "redeem"
    assert actions["v2"].action == "deposit"


def test_async_redemption_note():
    holdings = {"v2": 100_000.0}
    trades = compute_trades({"v2": 0.0}, 100_000, holdings, VAULTS)
    t = next(t for t in trades if t.vault_id == "v2")
    assert t.settlement == "async"
    assert "pending" in t.note.lower()


def test_dust_ignored():
    trades = compute_trades({"v1": 1.0}, 100_000, {"v1": 99_999.999}, VAULTS)
    assert trades == []


def test_dry_run_labeled_simulated():
    trades = compute_trades({"v1": 1.0}, 100_000, {}, VAULTS)
    report = dry_run_report(trades, VAULTS, 100_000, {"v1": 1.0})
    assert report["mode"] == "dry-run"
    assert "SIMULATED" in report["label"]


def test_dry_run_async_note():
    trades = compute_trades({"v2": 1.0}, 100_000, {}, VAULTS)
    report = dry_run_report(trades, VAULTS, 100_000, {"v2": 1.0})
    assert report["async_pending"] == 0  # it's a deposit, not redemption
    # Redeem
    holdings = {"v2": 100_000.0}
    trades2 = compute_trades({"v1": 1.0}, 100_000, holdings, VAULTS)
    report2 = dry_run_report(trades2, VAULTS, 100_000, {"v1": 1.0})
    assert report2["async_pending"] == 1
    assert report2["async_note"] is not None
