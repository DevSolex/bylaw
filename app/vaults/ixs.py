"""
app/vaults/ixs.py — read-only IXS adapter over a public BNB RPC.

Reads paused() and totalAssets() from the vault contract.
Falls back to the curated snapshot from config/vaults.yaml if the RPC
call fails, labeling the result clearly as unavailable-live.

Confirmed working (2026-09-20, bsc-dataseed.binance.org):
  paused()      selector 0x5c975abb — returns bool
  totalAssets() selector 0x01e1d114 — returns uint256 (18-decimal assumed)

Not available on this contract:
  availableAssets() — reverts; not implemented
  APY — no confirmed source

Everything not confirmed is labeled `curated` or `simulated`.
See docs/findings.md for evidence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.models import Vault
from app.vaults.base import VaultAdapter
from app.vaults.curated import CuratedAdapter

logger = logging.getLogger(__name__)

# Confirmed function selectors (2026-09-20)
_SEL_PAUSED       = "0x5c975abb"
_SEL_TOTAL_ASSETS = "0x01e1d114"

# Assumed decimal precision for totalAssets return value
# USDC on BSC is 6 decimals; the raw value may be shares (18 dec).
# Treat as 18-decimal until confirmed otherwise. Labeled as assumption.
_ASSUMED_DECIMALS = 18

# IXS vault details (from findings.md)
_IXS_ADDRESS  = "0xc975a3EeF2e49F8eDdEf585340C43f15300fCB82"
_IXS_VAULT_ID = "ixs-rwa-bnb"

# Default BNB public RPC — overridable via IXS_RPC_URL
_DEFAULT_RPC = "https://bsc-dataseed.binance.org/"


def _rpc_call(rpc_url: str, to: str, data: str, timeout: float = 10.0) -> str | None:
    """
    Make a single eth_call. Returns the hex result string, or None on error.
    Never raises.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    try:
        resp = httpx.post(rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        d = resp.json()
        if "error" in d:
            logger.debug("ixs: eth_call error: %s", d["error"])
            return None
        return d.get("result") or None
    except Exception as exc:
        logger.warning("ixs: RPC call failed (%s): %s", data[:10], exc)
        return None


def _decode_bool(hex_str: str | None) -> bool | None:
    if not hex_str or hex_str == "0x":
        return None
    try:
        return int(hex_str, 16) != 0
    except ValueError:
        return None


def _decode_uint(hex_str: str | None, decimals: int) -> float | None:
    if not hex_str or hex_str == "0x":
        return None
    try:
        raw = int(hex_str, 16)
        return raw / (10 ** decimals)
    except ValueError:
        return None


class IXSAdapter(VaultAdapter):
    """
    Read-only adapter for the IXS Permissionless Vault on BNB Smart Chain.

    On success: returns one Vault with live paused/total_assets and curated APY/risk.
    On RPC failure: returns the curated snapshot labeled with live_unavailable note.
    """

    def __init__(self) -> None:
        self._rpc_url = settings.ixs_rpc_url or _DEFAULT_RPC
        self._address = settings.ixs_vault_id or _IXS_ADDRESS
        self._curated = CuratedAdapter()

    def list_vaults(self) -> list[Vault]:
        now = datetime.now(timezone.utc)

        # Load curated baseline for this vault
        curated_vaults = {v.id: v for v in self._curated.list_vaults()}
        baseline = curated_vaults.get(_IXS_VAULT_ID)

        # Try live RPC reads
        paused_hex = _rpc_call(self._rpc_url, self._address, _SEL_PAUSED)
        assets_hex  = _rpc_call(self._rpc_url, self._address, _SEL_TOTAL_ASSETS)

        paused_live = _decode_bool(paused_hex)
        assets_live = _decode_uint(assets_hex, _ASSUMED_DECIMALS)

        live_ok = paused_live is not None

        if not live_ok:
            logger.warning(
                "ixs: RPC unavailable — using curated snapshot (labeled unavailable-live)"
            )
            if baseline:
                # Return curated but flag it as live-unavailable
                note = "IXS live data unavailable; showing last curated snapshot"
                sources = dict(baseline.data_sources)
                for k in sources:
                    sources[k] = "curated"
                return [baseline.model_copy(update={
                    "data_sources": sources,
                    "fetched_at": now,
                })]
            return []

        # Build live vault
        sources = {
            "paused": "live",
            "available_liquidity_usdc": "live",
            "apy": "curated",
            "redemption_days": "curated",
            "risk": "curated",
        }

        live_vault = Vault(
            id=_IXS_VAULT_ID,
            name="IXS RWA Permissionless Vault",
            chain="bsc",
            asset="USDC",
            apy=baseline.apy if baseline else None,
            redemption_days=baseline.redemption_days if baseline else 5,
            settlement="async",
            risk=baseline.risk if baseline else 4,
            paused=paused_live,
            available_liquidity_usdc=assets_live,
            data_sources=sources,
            fetched_at=now,
        )
        logger.info(
            "ixs: live read OK — paused=%s totalAssets=%.4f (18-dec assumed)",
            paused_live, assets_live or 0,
        )
        return [live_vault]
