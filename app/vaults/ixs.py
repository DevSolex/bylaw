"""
app/vaults/ixs.py — read-only IXS adapter over a public BNB RPC.

ON-CHAIN READS confirmed 2026-09-20 via bsc-dataseed.binance.org:
  paused()           0x5c975abb → bool                  — false
  totalAssets()      0x01e1d114 → uint256 (18-dec)       — 654.37 USDC
  asset()            0x38d52e0f → address                — 0x8ac76a51...580d (USDC on BSC)
  asset.decimals()   0x313ce567 → uint8                  — 18
  asset.symbol()     0x95d89b41 → string                 — "USDC"
  vault.symbol()     0x95d89b41 → string                 — "ixv1"
  pricePerShare()    0x99530b06 → uint256 (18-dec)       — ~1.0886

NOT AVAILABLE on this contract:
  availableAssets() → reverts (see docs/findings.md FINDING-IXS-1)
  Historical reads  → "missing trie node" (archive not served)

NAMING:
  totalAssets() is stored as total_assets_usdc. It is NOT available_liquidity
  (there is no availableAssets() function). It is the vault's total holdings.
  The capacity warning uses it as a proxy for vault size, not withdrawable liquidity.

YIELD:
  APY is an indicative figure from config/vaults.yaml (iShares SHYG 30-day SEC
  yield, 2026-09-17). pricePerShare is stored read-only for audit; yield is NOT
  derived from it because archive reads are unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.models import Vault
from app.vaults.base import VaultAdapter
from app.vaults.curated import CuratedAdapter

logger = logging.getLogger(__name__)

_SEL_PAUSED          = "0x5c975abb"
_SEL_TOTAL_ASSETS    = "0x01e1d114"
_SEL_ASSET           = "0x38d52e0f"
_SEL_DECIMALS        = "0x313ce567"
_SEL_SYMBOL          = "0x95d89b41"
_SEL_PRICE_PER_SHARE = "0x99530b06"

_IXS_ADDRESS  = "0xc975a3EeF2e49F8eDdEf585340C43f15300fCB82"
_IXS_VAULT_ID = "ixs-rwa-bnb"
_DEFAULT_RPC  = "https://bsc-dataseed.binance.org/"

CAPACITY_WARN_SHARE = 0.10   # default 10%; overridden by settings.capacity_warn_share


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc_call(rpc_url: str, to: str, data: str, timeout: float = 10.0) -> str | None:
    payload = {"jsonrpc": "2.0", "method": "eth_call",
               "params": [{"to": to, "data": data}, "latest"], "id": 1}
    try:
        resp = httpx.post(rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        d = resp.json()
        if "error" in d:
            logger.debug("ixs rpc error %s: %s", data[:10], d["error"])
            return None
        result = d.get("result") or ""
        return result if result and result != "0x" else None
    except Exception as exc:
        logger.warning("ixs: RPC call failed (%s): %s", data[:10], exc)
        return None


def _decode_uint(h: str | None, decimals: int = 0) -> int | float | None:
    if not h:
        return None
    try:
        raw = int(h, 16)
        return raw if decimals == 0 else raw / (10 ** decimals)
    except ValueError:
        return None


def _decode_bool(h: str | None) -> bool | None:
    v = _decode_uint(h, 0)
    return None if v is None else (v != 0)


def _decode_address(h: str | None) -> str | None:
    if not h or len(h) < 42:
        return None
    return "0x" + h[-40:].lower()


def _decode_uint8(h: str | None) -> int | None:
    v = _decode_uint(h, 0)
    return None if v is None else int(v)


def _decode_string(h: str | None) -> str | None:
    if not h or len(h) < 130:
        return None
    try:
        length = int(h[66:130], 16)
        return bytes.fromhex(h[130:130 + length * 2]).decode("utf-8", errors="replace")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IXSAdapter
# ---------------------------------------------------------------------------

class IXSAdapter(VaultAdapter):
    """
    Read-only adapter for the IXS Permissionless Vault on BNB Smart Chain.

    On each call reads: paused(), totalAssets(), asset() address + decimals +
    symbol, vault.symbol(), pricePerShare(). Falls back to curated snapshot
    on any RPC failure, with all sources relabeled 'curated'.
    """

    def __init__(self) -> None:
        self._rpc_url = settings.ixs_rpc_url or _DEFAULT_RPC
        self._vault_addr = _IXS_ADDRESS
        self._curated = CuratedAdapter()

    def list_vaults(self) -> list[Vault]:
        now = datetime.now(timezone.utc)
        curated_map = {v.id: v for v in self._curated.list_vaults()}
        baseline = curated_map.get(_IXS_VAULT_ID)
        reads = self._read_all(self._vault_addr)
        if not reads["live_ok"]:
            return self._fallback(baseline, now)
        return [self._build_live_vault(reads, baseline, now)]

    def _read_all(self, addr: str) -> dict:
        rpc = self._rpc_url
        r: dict = {"live_ok": False}

        paused_h     = _rpc_call(rpc, addr, _SEL_PAUSED)
        assets_h     = _rpc_call(rpc, addr, _SEL_TOTAL_ASSETS)
        asset_addr_h = _rpc_call(rpc, addr, _SEL_ASSET)
        vault_dec_h  = _rpc_call(rpc, addr, _SEL_DECIMALS)
        pps_h        = _rpc_call(rpc, addr, _SEL_PRICE_PER_SHARE)
        vault_sym_h  = _rpc_call(rpc, addr, _SEL_SYMBOL)

        paused    = _decode_bool(paused_h)
        vault_dec = _decode_uint8(vault_dec_h) or 18
        asset_addr = _decode_address(asset_addr_h)
        pps       = _decode_uint(pps_h, vault_dec)

        asset_dec = vault_dec
        asset_sym = "USDC"
        if asset_addr:
            adec_h = _rpc_call(rpc, asset_addr, _SEL_DECIMALS)
            asym_h = _rpc_call(rpc, asset_addr, _SEL_SYMBOL)
            asset_dec = _decode_uint8(adec_h) or vault_dec
            asset_sym = _decode_string(asym_h) or "USDC"

        total_assets = _decode_uint(assets_h, asset_dec) if assets_h else None
        vault_sym    = _decode_string(vault_sym_h)
        live_ok      = paused is not None

        r.update({
            "live_ok": live_ok,
            "paused": paused,
            "total_assets": total_assets,
            "asset_addr": asset_addr,
            "asset_dec": asset_dec,
            "asset_sym": asset_sym,
            "vault_dec": vault_dec,
            "vault_sym": vault_sym,
            "price_per_share": pps,
        })

        if live_ok:
            logger.info(
                "ixs: live | paused=%s | totalAssets=%.4f %s (%d dec) | "
                "asset=%s | vaultSymbol=%s | pricePerShare=%s",
                paused, total_assets or 0, asset_sym, asset_dec,
                asset_addr, vault_sym, f"{pps:.6f}" if pps else "n/a",
            )
        return r

    def _build_live_vault(self, r: dict, baseline: Vault | None, now: datetime) -> Vault:
        sources: dict[str, str] = {
            "paused":          "live",
            "total_assets_usdc": "live",
            "price_per_share": "live",
            "asset":           "live",
            "asset_decimals":  "live",
            "vault_symbol":    "live",
            "apy":             "curated",
            "redemption_days": "curated",
            "risk":            "curated",
        }
        return Vault(
            id=_IXS_VAULT_ID,
            name="IXS RWA Permissionless Vault",
            chain="bsc",
            asset=r["asset_sym"] or "USDC",
            apy=baseline.apy if baseline else None,
            apy_label=(baseline.apy_label if baseline else
                       "indicative: underlying ETF yield, not realized by the vault"),
            redemption_days=baseline.redemption_days if baseline else 5,
            settlement="async",
            risk=baseline.risk if baseline else 4,
            paused=r["paused"],
            total_assets_usdc=r["total_assets"],
            price_per_share=r["price_per_share"],
            live_read_at=now,
            as_of=baseline.as_of if baseline else None,
            data_sources=sources,  # type: ignore[arg-type]
            fetched_at=now,
        )

    def _fallback(self, baseline: Vault | None, now: datetime) -> list[Vault]:
        logger.warning("ixs: RPC unavailable — using curated snapshot")
        if not baseline:
            return []
        sources = {k: "curated" for k in baseline.data_sources}
        return [baseline.model_copy(update={
            "data_sources": sources,
            "live_read_at": None,
            "fetched_at": now,
        })]


# ---------------------------------------------------------------------------
# Capacity warning helper
# ---------------------------------------------------------------------------

def capacity_warnings(
    allocation: dict[str, float],
    amount_usdc: float,
    vaults: list[Vault],
    warn_share: float = CAPACITY_WARN_SHARE,
) -> list[dict]:
    """
    Return warning dicts when an allocation × amount exceeds warn_share of
    the vault's total_assets_usdc. Informational only — not a hard failure.

    NOTE: total_assets_usdc comes from totalAssets() on-chain, which is the
    vault's total holdings. It is NOT available/withdrawable liquidity
    (availableAssets() is not implemented on this contract).
    """
    vault_map = {v.id: v for v in vaults}
    warnings = []
    for vid, weight in allocation.items():
        v = vault_map.get(vid)
        if not v or v.total_assets_usdc is None:
            continue
        tvl = v.total_assets_usdc
        alloc_usdc = weight * amount_usdc
        if tvl <= 0:
            continue
        ratio = alloc_usdc / tvl
        if ratio > warn_share:
            warnings.append({
                "vault_id": vid,
                "vault_name": v.name,
                "allocation_usdc": round(alloc_usdc, 2),
                "tvl_usdc": round(tvl, 2),
                "allocation_pct_of_tvl": round(ratio * 100, 1),
                "threshold_pct": round(warn_share * 100, 1),
                "message": (
                    f"IXS vault currently holds ~{tvl:,.0f} {v.asset}. "
                    f"This allocation is {alloc_usdc:,.0f} {v.asset} "
                    f"({ratio:.1f}× the vault's total assets). "
                    f"The demo budget exceeds {warn_share:.0%} of real vault size."
                ),
            })
    return warnings
