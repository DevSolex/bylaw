"""
app/execution.py — compute trades and produce a dry-run execution report.

compute_trades(allocation, amount_usdc, holdings, vaults) → list[Trade]
dry_run_report(trades, vaults) → dict

The app never signs or submits transactions.
Async redemptions are shown as pending with an estimated timeline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.models import Holdings, Trade, Vault

logger = logging.getLogger(__name__)


def compute_trades(
    allocation: dict[str, float],
    amount_usdc: float,
    holdings: Holdings,
    vaults: list[Vault],
) -> list[Trade]:
    """
    Convert an approved allocation into a list of deposits and redemptions.

    Parameters
    ----------
    allocation : vault_id → weight (0-1, sum ≈ 1)
    amount_usdc: total portfolio size in USDC
    holdings   : current vault_id → amount_usdc positions (empty = all cash)
    vaults     : vault snapshot for settlement metadata
    """
    vault_map = {v.id: v for v in vaults}
    trades: list[Trade] = []

    target_usdc = {vid: w * amount_usdc for vid, w in allocation.items()}

    all_ids = set(target_usdc) | set(holdings)
    for vid in all_ids:
        target = target_usdc.get(vid, 0.0)
        current = holdings.get(vid, 0.0)
        delta = target - current
        if abs(delta) < 0.01:       # ignore dust
            continue
        vault = vault_map.get(vid)
        settlement = vault.settlement if vault else None
        redemption_days = vault.redemption_days if vault else None
        action: str = "deposit" if delta > 0 else "redeem"
        note = _trade_note(action, settlement, redemption_days)
        trades.append(Trade(
            vault_id=vid,
            action=action,  # type: ignore[arg-type]
            amount_usdc=round(abs(delta), 2),
            settlement=settlement,
            note=note,
        ))

    return sorted(trades, key=lambda t: t.vault_id)


def _trade_note(action: str, settlement: str | None, redemption_days: int | None) -> str:
    if action == "deposit":
        if settlement == "async":
            return "async deposit: pending until operator confirms"
        return "sync deposit: expected to settle immediately"
    else:
        if settlement == "async":
            days = f"~{redemption_days} days" if redemption_days else "unknown duration"
            return (
                f"async redemption: pending — estimated {days}. "
                f"Funds will not be available until the operator finalises the request."
            )
        return "sync redemption: expected to settle immediately"


def dry_run_report(
    trades: list[Trade],
    vaults: list[Vault],
    amount_usdc: float,
    allocation: dict[str, float],
) -> dict:
    """
    Produce a labelled dry-run execution report.
    Never submits anything. All results are labeled 'simulated'.
    """
    vault_map = {v.id: v for v in vaults}
    now = datetime.now(timezone.utc).isoformat()

    trade_items = []
    for t in trades:
        vault = vault_map.get(t.vault_id)
        trade_items.append({
            "vault_id": t.vault_id,
            "vault_name": vault.name if vault else t.vault_id,
            "action": t.action,
            "amount_usdc": t.amount_usdc,
            "settlement": t.settlement,
            "note": t.note,
            "status": "pending" if t.settlement == "async" else "simulated-complete",
        })

    async_redemptions = [t for t in trades if t.action == "redeem" and t.settlement == "async"]

    return {
        "mode": "dry-run",
        "label": "SIMULATED — no transactions were submitted",
        "executed_at": now,
        "total_amount_usdc": amount_usdc,
        "trades": trade_items,
        "async_pending": len(async_redemptions),
        "async_note": (
            "Async redemptions are shown as pending. "
            "Actual settlement depends on vault operator timelines."
        ) if async_redemptions else None,
        "summary": (
            f"{len(trades)} trade(s): "
            f"{sum(1 for t in trades if t.action=='deposit')} deposit(s), "
            f"{sum(1 for t in trades if t.action=='redeem')} redemption(s)."
        ),
    }
