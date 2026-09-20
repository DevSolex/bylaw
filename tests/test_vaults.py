"""
tests/test_vaults.py — adapter tests using recorded fixtures (no network).

Covers:
  - MockAdapter: 5 vaults, correct archetypes, all fields labeled simulated
  - CuratedAdapter: loads vaults.yaml, correct field values
  - IXSAdapter: live RPC mocked with respx; fallback on failure
  - Dispatcher: simulated/mixed/live modes
  - Source labels present on every vault
  - Paused vault present and labeled
  - Async-settlement vault present
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
import httpx

from app.models import Vault
from app.vaults.mock import MockAdapter, build_mock_vaults
from app.vaults.curated import CuratedAdapter
from app.vaults.ixs import IXSAdapter, _IXS_VAULT_ID, _DEFAULT_RPC, _IXS_ADDRESS
from app.vaults import get_vaults

# ---------------------------------------------------------------------------
# Recorded BNB RPC fixture responses
# ---------------------------------------------------------------------------

# paused() → false (0)
_PAUSED_FALSE = "0x0000000000000000000000000000000000000000000000000000000000000000"
# totalAssets() → 654372319588990851422 raw (18-dec → ~654.37 units)
_TOTAL_ASSETS  = "0x000000000000000000000000000000000000000000000023793d7da72e30195e"


def _mock_rpc(paused_result: str = _PAUSED_FALSE,
              assets_result: str = _TOTAL_ASSETS,
              fail: bool = False):
    """Context manager that intercepts httpx POST to the BSC RPC."""
    if fail:
        return respx.mock(base_url=_DEFAULT_RPC)
    router = respx.mock()

    def _handler(request: httpx.Request):
        body = request.content.decode()
        if '"paused"' in body or "5c975abb" in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": paused_result, "id": 1})
        if "01e1d114" in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": assets_result, "id": 1})
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": "0x", "id": 1})

    router.post(_DEFAULT_RPC).mock(side_effect=_handler)
    return router


# ---------------------------------------------------------------------------
# MockAdapter
# ---------------------------------------------------------------------------

class TestMockAdapter:
    def test_returns_five_vaults(self):
        vaults = MockAdapter().list_vaults()
        assert len(vaults) == 5

    def test_all_fields_labeled_simulated_or_curated(self):
        for v in MockAdapter().list_vaults():
            assert v.data_sources, f"{v.id} has no data_sources"
            for field, label in v.data_sources.items():
                assert label in ("simulated", "curated"), \
                    f"{v.id}.{field} = {label!r}"

    def test_paused_vault_present(self):
        vaults = MockAdapter().list_vaults()
        paused = [v for v in vaults if v.paused is True]
        assert len(paused) >= 1, "no paused vault in mock set"

    def test_async_settlement_vault_present(self):
        vaults = MockAdapter().list_vaults()
        async_vaults = [v for v in vaults if v.settlement == "async"]
        assert len(async_vaults) >= 1

    def test_instant_redemption_vault_present(self):
        vaults = MockAdapter().list_vaults()
        instant = [v for v in vaults if v.redemption_days == 0]
        assert len(instant) >= 1

    def test_risk_scores_in_range(self):
        for v in MockAdapter().list_vaults():
            assert 1 <= v.risk <= 5

    def test_fetched_at_is_recent(self):
        now = datetime.now(timezone.utc)
        for v in MockAdapter().list_vaults():
            delta = abs((now - v.fetched_at).total_seconds())
            assert delta < 60, f"{v.id} fetched_at too old"


# ---------------------------------------------------------------------------
# CuratedAdapter
# ---------------------------------------------------------------------------

class TestCuratedAdapter:
    def setup_method(self):
        yaml_path = Path(__file__).parent.parent / "config" / "vaults.yaml"
        self.adapter = CuratedAdapter(yaml_path=yaml_path)

    def test_loads_ixs_vault(self):
        vaults = self.adapter.list_vaults()
        ids = [v.id for v in vaults]
        assert "ixs-rwa-bnb" in ids

    def test_loads_at_least_five_vaults(self):
        assert len(self.adapter.list_vaults()) >= 5

    def test_ixs_vault_fields(self):
        vaults = {v.id: v for v in self.adapter.list_vaults()}
        ixs = vaults["ixs-rwa-bnb"]
        assert ixs.chain == "bsc"
        assert ixs.settlement == "async"
        assert ixs.risk == 4

    def test_paused_vault_in_curated(self):
        vaults = self.adapter.list_vaults()
        paused = [v for v in vaults if v.paused is True]
        assert len(paused) >= 1

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
    def test_live_read_returns_ixs_vault(self):
        with _mock_rpc():
            vaults = IXSAdapter().list_vaults()
        assert len(vaults) == 1
        assert vaults[0].id == _IXS_VAULT_ID

    def test_live_paused_is_false(self):
        with _mock_rpc(paused_result=_PAUSED_FALSE):
            vaults = IXSAdapter().list_vaults()
        assert vaults[0].paused is False

    def test_live_paused_is_true(self):
        paused_true = "0x" + "0" * 63 + "1"
        with _mock_rpc(paused_result=paused_true):
            vaults = IXSAdapter().list_vaults()
        assert vaults[0].paused is True

    def test_total_assets_decoded(self):
        with _mock_rpc():
            vaults = IXSAdapter().list_vaults()
        # ~654.37 at 18 decimals
        assert vaults[0].available_liquidity_usdc is not None
        assert 600 < vaults[0].available_liquidity_usdc < 700

    def test_live_fields_labeled_live(self):
        with _mock_rpc():
            vaults = IXSAdapter().list_vaults()
        src = vaults[0].data_sources
        assert src.get("paused") == "live"
        assert src.get("available_liquidity_usdc") == "live"

    def test_curated_fields_labeled_curated(self):
        with _mock_rpc():
            vaults = IXSAdapter().list_vaults()
        src = vaults[0].data_sources
        assert src.get("risk") == "curated"

    def test_fallback_on_rpc_failure(self):
        """When RPC fails, adapter returns curated snapshot (not empty)."""
        with respx.mock():
            respx.post(_DEFAULT_RPC).mock(
                side_effect=httpx.ConnectError("refused")
            )
            vaults = IXSAdapter().list_vaults()
        # Falls back to curated; should return the IXS vault from vaults.yaml
        assert len(vaults) >= 1
        assert vaults[0].id == _IXS_VAULT_ID
        # All sources should be curated after fallback
        for label in vaults[0].data_sources.values():
            assert label == "curated"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_simulated_mode(self):
        vaults = get_vaults("simulated")
        assert len(vaults) >= 4

    def test_simulated_all_labeled(self):
        for v in get_vaults("simulated"):
            for label in v.data_sources.values():
                assert label in ("simulated", "curated")

    def test_mixed_mode_includes_peers(self):
        with _mock_rpc():
            vaults = get_vaults("mixed")
        ids = [v.id for v in vaults]
        assert _IXS_VAULT_ID in ids
        # At least one mock peer
        mock_ids = [i for i in ids if i.startswith("mock-")]
        assert len(mock_ids) >= 1

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="data_mode"):
            get_vaults("invalid")
