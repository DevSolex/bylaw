"""
tests/test_vaults.py — adapter tests using recorded fixtures (no network).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import respx
import httpx

from app.models import Vault
from app.vaults.mock import MockAdapter, build_mock_vaults
from app.vaults.curated import CuratedAdapter
from app.vaults.ixs import (
    IXSAdapter, _IXS_VAULT_ID, _DEFAULT_RPC,
    capacity_warnings, CAPACITY_WARN_SHARE,
)
from app.vaults import get_vaults

# ---------------------------------------------------------------------------
# Recorded BNB RPC responses (confirmed 2026-09-20)
# ---------------------------------------------------------------------------

_PAUSED_FALSE  = "0x" + "0" * 64
_PAUSED_TRUE   = "0x" + "0" * 63 + "1"
_TOTAL_ASSETS  = "0x000000000000000000000000000000000000000000000023793d7da72e30195e"
# asset() returns the USDC address, ABI-padded to 32 bytes
_ASSET_ADDR    = "0x0000000000000000000000008ac76a51cc950d9822d68b83fe1ad97b32cd580d"
# decimals() = 18 = 0x12
_DECIMALS_18   = "0x" + "0" * 62 + "12"
# symbol() "USDC" ABI-encoded string
_USDC_SYMBOL   = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "5553444300000000000000000000000000000000000000000000000000000000"
)
# pricePerShare ~1.0886 (0x0f1b784b5be94000)
_PRICE_PER_SHARE = "0x000000000000000000000000000000000000000000000000" + "0f1b784b5be94000"


def _make_rpc_handler(
    paused: str = _PAUSED_FALSE,
    assets: str = _TOTAL_ASSETS,
    fail: bool = False,
):
    """Return a respx side-effect that routes by selector."""
    def _handler(request: httpx.Request):
        if fail:
            raise httpx.ConnectError("refused")
        body = request.content.decode()
        # Route by selector in the 'data' field
        if "5c975abb" in body:   # paused()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":paused,"id":1})
        if "01e1d114" in body:   # totalAssets()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":assets,"id":1})
        if "38d52e0f" in body:   # asset()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":_ASSET_ADDR,"id":1})
        if "313ce567" in body:   # decimals()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":_DECIMALS_18,"id":1})
        if "95d89b41" in body:   # symbol()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":_USDC_SYMBOL,"id":1})
        if "99530b06" in body:   # pricePerShare()
            return httpx.Response(200, json={"jsonrpc":"2.0","result":_PRICE_PER_SHARE,"id":1})
        # Anything else (name, convertToAssets, …) — return empty
        return httpx.Response(200, json={"jsonrpc":"2.0","result":"0x","id":1})
    return _handler


# ---------------------------------------------------------------------------
# MockAdapter
# ---------------------------------------------------------------------------

class TestMockAdapter:
    def test_returns_five_vaults(self):
        assert len(MockAdapter().list_vaults()) == 5

    def test_all_fields_labeled_simulated_or_curated(self):
        for v in MockAdapter().list_vaults():
            assert v.data_sources
            for label in v.data_sources.values():
                assert label in ("simulated", "curated")

    def test_paused_vault_present(self):
        assert any(v.paused for v in MockAdapter().list_vaults())

    def test_async_settlement_vault_present(self):
        assert any(v.settlement == "async" for v in MockAdapter().list_vaults())

    def test_instant_redemption_vault_present(self):
        assert any(v.redemption_days == 0 for v in MockAdapter().list_vaults())

    def test_risk_scores_in_range(self):
        for v in MockAdapter().list_vaults():
            assert 1 <= v.risk <= 5

    def test_fetched_at_is_recent(self):
        now = datetime.now(timezone.utc)
        for v in MockAdapter().list_vaults():
            assert abs((now - v.fetched_at).total_seconds()) < 60


# ---------------------------------------------------------------------------
# CuratedAdapter
# ---------------------------------------------------------------------------

class TestCuratedAdapter:
    def setup_method(self):
        yaml_path = Path(__file__).parent.parent / "config" / "vaults.yaml"
        self.adapter = CuratedAdapter(yaml_path=yaml_path)

    def test_loads_ixs_vault(self):
        ids = [v.id for v in self.adapter.list_vaults()]
        assert "ixs-rwa-bnb" in ids

    def test_loads_at_least_five_vaults(self):
        assert len(self.adapter.list_vaults()) >= 5

    def test_ixs_vault_fields(self):
        vmap = {v.id: v for v in self.adapter.list_vaults()}
        ixs = vmap["ixs-rwa-bnb"]
        assert ixs.chain == "bsc"
        assert ixs.settlement == "async"
        assert ixs.risk == 4

    def test_paused_vault_in_curated(self):
        assert any(v.paused for v in self.adapter.list_vaults())

    def test_all_vaults_have_data_sources(self):
        for v in self.adapter.list_vaults():
            assert v.data_sources, f"{v.id} missing data_sources"

    def test_missing_yaml_returns_empty(self, tmp_path):
        adapter = CuratedAdapter(yaml_path=tmp_path / "nonexistent.yaml")
        assert adapter.list_vaults() == []


# ---------------------------------------------------------------------------
# IXSAdapter (mocked RPC)
# ---------------------------------------------------------------------------

class TestIXSAdapter:
    def _live(self, paused=_PAUSED_FALSE, assets=_TOTAL_ASSETS):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=_make_rpc_handler(paused, assets))
        return router

    def test_live_read_returns_ixs_vault(self):
        with self._live():
            vaults = IXSAdapter().list_vaults()
        assert len(vaults) == 1
        assert vaults[0].id == _IXS_VAULT_ID

    def test_live_paused_false(self):
        with self._live(paused=_PAUSED_FALSE):
            v = IXSAdapter().list_vaults()[0]
        assert v.paused is False

    def test_live_paused_true(self):
        with self._live(paused=_PAUSED_TRUE):
            v = IXSAdapter().list_vaults()[0]
        assert v.paused is True

    def test_total_assets_decoded_with_confirmed_decimals(self):
        """totalAssets / 10^18 should be ~654.37 (BSC USDC = 18 dec, confirmed)."""
        with self._live():
            v = IXSAdapter().list_vaults()[0]
        assert v.available_liquidity_usdc is not None
        assert 600 < v.available_liquidity_usdc < 700

    def test_asset_symbol_read_live(self):
        """asset() and asset.symbol() should return 'USDC'."""
        with self._live():
            v = IXSAdapter().list_vaults()[0]
        assert v.asset == "USDC"

    def test_live_fields_labeled_live(self):
        with self._live():
            v = IXSAdapter().list_vaults()[0]
        assert v.data_sources["paused"] == "live"
        assert v.data_sources["available_liquidity_usdc"] == "live"

    def test_curated_fields_labeled_curated(self):
        with self._live():
            v = IXSAdapter().list_vaults()[0]
        assert v.data_sources["risk"] == "curated"

    def test_fallback_on_rpc_failure(self):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=httpx.ConnectError("refused"))
        with router:
            vaults = IXSAdapter().list_vaults()
        assert len(vaults) >= 1
        assert vaults[0].id == _IXS_VAULT_ID
        for label in vaults[0].data_sources.values():
            assert label == "curated"

    def test_apy_is_none_live(self):
        """APY must be None for live reads — no confirmed source."""
        with self._live():
            v = IXSAdapter().list_vaults()[0]
        assert v.apy is None


# ---------------------------------------------------------------------------
# Capacity warnings
# ---------------------------------------------------------------------------

class TestCapacityWarnings:
    def _now(self):
        return datetime.now(timezone.utc)

    def test_no_warning_within_threshold(self):
        v = Vault(id="v1", name="V1", chain="eth", asset="USDC",
                  risk=2, available_liquidity_usdc=1_000_000,
                  fetched_at=self._now())
        # 5% of 100k = 5k, TVL 1M → 0.5% share < 10% threshold
        warns = capacity_warnings({"v1": 0.05}, 100_000, [v])
        assert warns == []

    def test_warning_when_exceeds_threshold(self):
        v = Vault(id="ixs-rwa-bnb", name="IXS", chain="bsc", asset="USDC",
                  risk=4, available_liquidity_usdc=654,
                  fetched_at=self._now())
        # 50% of 100k = 50k, TVL 654 → way over 10%
        warns = capacity_warnings({"ixs-rwa-bnb": 0.5}, 100_000, [v])
        assert len(warns) == 1
        assert "ixs-rwa-bnb" in warns[0]["vault_id"]
        assert warns[0]["allocation_pct_of_tvl"] > 100

    def test_warning_message_mentions_demo_budget(self):
        v = Vault(id="v1", name="Small Vault", chain="eth", asset="USDC",
                  risk=2, available_liquidity_usdc=500,
                  fetched_at=self._now())
        warns = capacity_warnings({"v1": 0.2}, 100_000, [v])
        assert warns
        assert "demo budget" in warns[0]["message"].lower() or \
               "tvl" in warns[0]["message"].lower()

    def test_no_warning_when_no_tvl_data(self):
        v = Vault(id="v1", name="V1", chain="eth", asset="USDC",
                  risk=2, available_liquidity_usdc=None,
                  fetched_at=self._now())
        warns = capacity_warnings({"v1": 0.5}, 100_000, [v])
        assert warns == []


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_simulated_mode(self):
        assert len(get_vaults("simulated")) >= 4

    def test_simulated_all_labeled(self):
        for v in get_vaults("simulated"):
            for label in v.data_sources.values():
                assert label in ("simulated", "curated")

    def test_mixed_mode_includes_ixs_and_peers(self):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=_make_rpc_handler())
        with router:
            vaults = get_vaults("mixed")
        ids = [v.id for v in vaults]
        assert _IXS_VAULT_ID in ids
        assert any(i.startswith("mock-") for i in ids)

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="data_mode"):
            get_vaults("invalid")
