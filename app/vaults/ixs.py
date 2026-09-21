"""
app/vaults/ixs.py — read-only IXS adapter over a public BNB RPC.

ON-CHAIN READS (all confirmed 2026-09-20 via bsc-dataseed.binance.org):
  paused()                    0x5c975abb → bool
  totalAssets()               0x01e1d114 → uint256
  asset()                     0x38d52e0f → address  (= 0x8ac76a51...580d, USDC on BSC)
  asset.decimals()            0x313ce567 → uint8    (= 18 — BSC USDC uses 18 dec)
  asset.symbol()              0x95d89b41 → string   (= "USDC")
  asset.name()                0x06fdde03 → string   (= "USD Coin")
  vault.decimals()            0x313ce567 → uint8    (= 18)
  vault.symbol()              0x95d89b41 → string   (= "ixv1")
  pricePerShare()             0x99530b06 → uint256  (~1.0886 USDC/share at read time)
  convertToAssets(1e18)       0x07a2d13a → uint256  (same as pricePerShare)

HISTORICAL READS:
  pricePerShare at past blocks → "missing trie node" — public RPC does not serve
  archive state. Trailing yield cannot be derived on-chain from this endpoint.
  See docs/findings.md FINDING-IXS-4.

APY:
  Indicative figure derived from current pricePerShare only:
  If price > 1.0 the vault has accrued yield since inception; we cannot compute
  an annualized rate without a start date. We use a labeled "indicative" value
  from the BNB chain data until a better source is available.
  Never mixed with a "realized" or "live" label.

CAPACITY WARNING:
  When an allocation exceeds CAPACITY_WARN_SHARE of totalAssets, a warning is
  added to the vault record. This is informational only — not a hard failure.
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

# ── Confirmed selectors (2026-09-20) ──────────────────────────────────────
_SEL_PAUSED            = "0x5c975abb"  # paused() → bool
_SEL_TOTAL_ASSETS      = "0x01e1d114"  # totalAssets() → uint256
_SEL_ASSET             = "0x38d52e0f"  # asset() → address
_SEL_DECIMALS          = "0x313ce567"  # decimals() → uint8
_SEL_SYMBOL            = "0x95d89b41"  # symbol() → string
_SEL_NAME              = "0x06fdde03"  # name() → string
_SEL_PRICE_PER_SHARE   = "0x99530b06"  # pricePerShare() → uint256

# ── Contract details ───────────────────────────────────────────────────────
_IXS_ADDRESS  = "0xc975a3EeF2e49F8eDdEf585340C43f15300fCB82"
_IXS_VAULT_ID = "ixs-rwa-bnb"
_DEFAULT_RPC  = "https://bsc-dataseed.binance.org/"

# Capacity warning threshold — warn when allocation > this share of TVL
CAPACITY_WARN_SHARE = 0.10   # 10%

# Confirmed BSC USDC: 0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d
_KNOWN_BSC_USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"

# Indicative APY figure (see module docstring and findings.md FINDING-IXS-3/4)
# Source: share price growth implied by pricePerShare > 1.0 as of 2026-09-20.
# Exact annualised rate cannot be computed without archive data.
# This is a PLACEHOLDER labeled "indicative" — not realized, not confirmed.
_INDICATIVE_APY = None   # set to None until a reliable source is confirmed


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc_call(rpc_url: str, to: str, data: str, timeout: float = 10.0) -> str | None:
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"], "id": 1,
    }
    try:
        resp = httpx.post(rpc_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        d = resp.json()
        if "error" in d:
            logger.debug("ixs rpc error for %s: %s", data[:10], d["error"])
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
    """Extract an address from a 32-byte ABI-encoded value."""
    if not h or len(h) < 42:
        return None
    return "0x" + h[-40:].lower()


def _decode_uint8(h: str | None) -> int | None:
    v = _decode_uint(h, 0)
    return None if v is None else int(v)


def _decode_string(h: str | None) -> str | None:
    """Decode an ABI-encoded string (dynamic type)."""
    if not h or len(h) < 130:
        return None
    try:
        length = int(h[66:130], 16)
        raw_bytes = bytes.fromhex(h[130:130 + length * 2])
        return raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IXSAdapter
# ---------------------------------------------------------------------------

class IXSAdapter(VaultAdapter):
    """
    Read-only adapter for the IXS Permissionless Vault on BNB Smart Chain.

    Reads on each call:
      - paused()
      - totalAssets()
      - asset() address, then asset.decimals() and asset.symbol()
      - vault.decimals(), vault.symbol()
      - pricePerShare()

    Falls back to the curated snapshot on any RPC failure.
    """

    def __init__(self) -> None:
        self._rpc_url = settings.ixs_rpc_url or _DEFAULT_RPC
        self._vault_addr = _IXS_ADDRESS   # IXS_VAULT_ID config holds a vault id, not address
        self._curated = CuratedAdapter()

    def list_vaults(self) -> list[Vault]:
        now = datetime.now(timezone.utc)
        curated_map = {v.id: v for v in self._curated.list_vaults()}
        baseline = curated_map.get(_IXS_VAULT_ID)

        reads = self._read_all(self._vault_addr)

        if not reads["live_ok"]:
            return self._fallback(baseline, now)

        return [self._build_live_vault(reads, baseline, now)]

    # ------------------------------------------------------------------

    def _read_all(self, addr: str) -> dict:
        rpc = self._rpc_url
        r: dict = {"live_ok": False}

        # Core reads
        paused_h     = _rpc_call(rpc, addr, _SEL_PAUSED)
        assets_h     = _rpc_call(rpc, addr, _SEL_TOTAL_ASSETS)
        asset_addr_h = _rpc_call(rpc, addr, _SEL_ASSET)
        vault_dec_h  = _rpc_call(rpc, addr, _SEL_DECIMALS)
        pps_h        = _rpc_call(rpc, addr, _SEL_PRICE_PER_SHARE)
        vault_sym_h  = _rpc_call(rpc, addr, _SEL_SYMBOL)

        paused     = _decode_bool(paused_h)
        vault_dec  = _decode_uint8(vault_dec_h) or 18
        asset_addr = _decode_address(asset_addr_h)
        pps        = _decode_uint(pps_h, vault_dec)

        # Asset contract reads
        asset_dec = vault_dec   # default to vault decimals if asset unreachable
        asset_sym = "USDC"      # default
        if asset_addr:
            adec_h = _rpc_call(rpc, asset_addr, _SEL_DECIMALS)
            asym_h = _rpc_call(rpc, asset_addr, _SEL_SYMBOL)
            asset_dec = _decode_uint8(adec_h) or vault_dec
            asset_sym = _decode_string(asym_h) or "USDC"

        total_assets = _decode_uint(assets_h, asset_dec) if assets_h else None
        vault_sym    = _decode_string(vault_sym_h)

        live_ok = paused is not None  # minimum viable read

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
                "ixs: live read OK | paused=%s | totalAssets=%.4f %s (%d dec) | "
                "asset=%s | vaultSymbol=%s | pricePerShare=%s",
                paused, total_assets or 0, asset_sym, asset_dec,
                asset_addr, vault_sym, f"{pps:.6f}" if pps else "n/a",
            )

        return r

    def _build_live_vault(self, r: dict, baseline: Vault | None, now: datetime) -> Vault:
        total_assets = r["total_assets"]

        # Capacity warning metadata (stored in data_sources for UI access)
        sources: dict[str, str] = {
            "paused":                    "live",
            "available_liquidity_usdc":  "live",
            "asset":                     "live",
            "asset_decimals":            "live",
            "vault_symbol":              "live",
            "price_per_share":           "live",
            "apy":                       "curated",
            "redemption_days":           "curated",
            "risk":                      "curated",
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
            available_liquidity_usdc=total_assets,
            data_sources=sources,  # type: ignore[arg-type]
            fetched_at=now,
        )

    def _fallback(self, baseline: Vault | None, now: datetime) -> list[Vault]:
        logger.warning("ixs: RPC unavailable — using curated snapshot")
        if not baseline:
            return []
        sources = {k: "curated" for k in baseline.data_sources}
        sources["live_status"] = "curated"   # marker for UI banner
        return [baseline.model_copy(update={"data_sources": sources, "fetched_at": now})]


# ---------------------------------------------------------------------------
# Capacity warning helper (used by the API layer)
# ---------------------------------------------------------------------------

def capacity_warnings(
    allocation: dict[str, float],
    amount_usdc: float,
    vaults: list[Vault],
    warn_share: float = CAPACITY_WARN_SHARE,
) -> list[dict]:
    """
    Return a list of warning dicts when an allocation exceeds warn_share
    of the vault's totalAssets (available_liquidity_usdc).

    These are informational only — not hard failures.
    """
    vault_map = {v.id: v for v in vaults}
    warnings = []
    for vid, weight in allocation.items():
        v = vault_map.get(vid)
        if not v or v.available_liquidity_usdc is None:
            continue
        amount_in = weight * amount_usdc
        share = amount_in / v.available_liquidity_usdc if v.available_liquidity_usdc > 0 else 0
        if share > warn_share:
            warnings.append({
                "vault_id": vid,
                "vault_name": v.name,
                "allocation_usdc": round(amount_in, 2),
                "tvl_usdc": round(v.available_liquidity_usdc, 2),
                "allocation_pct_of_tvl": round(share * 100, 1),
                "threshold_pct": round(warn_share * 100, 1),
                "message": (
                    f"Allocation of {amount_in:,.0f} {v.asset} to {v.name} is "
                    f"{share:.0%} of the vault's TVL ({v.available_liquidity_usdc:,.0f} {v.asset}). "
                    f"This demo budget exceeds {warn_share:.0%} of real vault size."
                ),
            })
    return warnings
