"""
tests/test_pre_m6.py — targeted tests for pre-M6 fixes.

Covers:
  - IXS on-chain field parsing (decimals, asset address, symbol) via recorded fixtures
  - total_assets_usdc rename (no available_liquidity_usdc on Vault)
  - price_per_share stored read-only on Vault
  - as_of and live_read_at populated correctly
  - capacity_warnings: threshold logic, message content, ratio display
  - Per-step token accounting: policy_parse vs policy_repair vs proposal_N
  - Alias vs truly-unknown-key repair behavior
  - IXS APY label present and correctly worded
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import respx
import httpx

from app.models import ModelMeta, Policy, Vault
from app.policy import parse_policy, _KEY_ALIASES
from app.vaults.curated import CuratedAdapter
from app.vaults.ixs import IXSAdapter, _IXS_VAULT_ID, _DEFAULT_RPC, capacity_warnings
from tests.test_serv_client import FakeServClient

_NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Recorded RPC fixture responses (confirmed 2026-09-20)
# ---------------------------------------------------------------------------

_PAUSED_FALSE    = "0x" + "0" * 64
_TOTAL_ASSETS    = "0x000000000000000000000000000000000000000000000023793d7da72e30195e"
_ASSET_ADDR      = "0x0000000000000000000000008ac76a51cc950d9822d68b83fe1ad97b32cd580d"
_DECIMALS_18     = "0x" + "0" * 62 + "12"
_USDC_SYMBOL     = (
    "0x"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "5553444300000000000000000000000000000000000000000000000000000000"
)
_PRICE_PER_SHARE = "0x" + "0" * 48 + "0f1b784b5be94000"


def _rpc_handler(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "5c975abb" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _PAUSED_FALSE, "id": 1})
    if "01e1d114" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _TOTAL_ASSETS, "id": 1})
    if "38d52e0f" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _ASSET_ADDR, "id": 1})
    if "313ce567" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _DECIMALS_18, "id": 1})
    if "95d89b41" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _USDC_SYMBOL, "id": 1})
    if "99530b06" in body:
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": _PRICE_PER_SHARE, "id": 1})
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": "0x", "id": 1})


# ---------------------------------------------------------------------------
# IXS on-chain field parsing
# ---------------------------------------------------------------------------

class TestIXSOnChainParsing:
    def _adapter(self):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=_rpc_handler)
        return router

    def test_decimals_read_live_not_assumed(self):
        with self._adapter():
            vaults = IXSAdapter().list_vaults()
        # asset_dec=18 must be reflected in total_assets_usdc (not a 6-dec value)
        assert vaults[0].total_assets_usdc is not None
        assert 600 < vaults[0].total_assets_usdc < 700  # ~654 at 18 dec

    def test_asset_symbol_usdc(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.asset == "USDC"

    def test_price_per_share_stored(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        # pricePerShare ~1.0886
        assert v.price_per_share is not None
        assert 1.0 < v.price_per_share < 1.2

    def test_price_per_share_labeled_live(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.data_sources.get("price_per_share") == "live"

    def test_total_assets_labeled_live(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.data_sources.get("total_assets_usdc") == "live"

    def test_no_field_named_available_liquidity(self):
        """The old field name must not exist anywhere on Vault."""
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert not hasattr(v, "available_liquidity_usdc")

    def test_live_read_at_populated(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.live_read_at is not None

    def test_as_of_from_curated(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        # as_of comes from vaults.yaml
        assert v.as_of is not None
        assert "2026" in v.as_of

    def test_fallback_clears_live_read_at(self):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=httpx.ConnectError("refused"))
        with router:
            vaults = IXSAdapter().list_vaults()
        assert vaults[0].live_read_at is None


# ---------------------------------------------------------------------------
# IXS APY label
# ---------------------------------------------------------------------------

class TestIXSAPYLabel:
    def _adapter(self):
        router = respx.mock()
        router.post(_DEFAULT_RPC).mock(side_effect=_rpc_handler)
        return router

    def test_apy_label_present(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.apy_label is not None
        assert len(v.apy_label) > 10

    def test_apy_label_says_indicative(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert "indicative" in v.apy_label.lower()

    def test_apy_label_says_not_realized(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert "not realized" in v.apy_label.lower()

    def test_apy_source_is_curated_not_live(self):
        with self._adapter():
            v = IXSAdapter().list_vaults()[0]
        assert v.data_sources.get("apy") == "curated"

    def test_curated_apy_value(self):
        """IXS APY should be ~6.72% from vaults.yaml."""
        from pathlib import Path
        curated = CuratedAdapter().list_vaults()
        ixs = next(v for v in curated if v.id == _IXS_VAULT_ID)
        assert ixs.apy is not None
        assert abs(ixs.apy - 0.0672) < 0.001


# ---------------------------------------------------------------------------
# Capacity warnings
# ---------------------------------------------------------------------------

def _vault(id="v1", tvl=None):
    return Vault(id=id, name=id, chain="eth", asset="USDC",
                 risk=2, total_assets_usdc=tvl, fetched_at=_NOW)


class TestCapacityWarnings:
    def test_no_warning_within_threshold(self):
        v = _vault("v1", tvl=1_000_000)
        warns = capacity_warnings({"v1": 0.05}, 100_000, [v])
        assert warns == []

    def test_warning_fires_above_threshold(self):
        v = _vault("ixs-rwa-bnb", tvl=654)
        warns = capacity_warnings({"ixs-rwa-bnb": 0.5}, 500, [v])
        assert len(warns) == 1

    def test_warning_message_contains_tvl(self):
        v = _vault("ixs-rwa-bnb", tvl=654)
        warns = capacity_warnings({"ixs-rwa-bnb": 0.5}, 500, [v])
        msg = warns[0]["message"]
        assert "654" in msg

    def test_warning_message_contains_ratio(self):
        v = _vault("ixs-rwa-bnb", tvl=654)
        warns = capacity_warnings({"ixs-rwa-bnb": 0.5}, 1000, [v])
        # 500 / 654 ≈ 0.76×
        assert "×" in warns[0]["message"] or "x" in warns[0]["message"].lower()

    def test_no_warning_when_no_tvl(self):
        v = _vault("v1", tvl=None)
        warns = capacity_warnings({"v1": 0.9}, 100_000, [v])
        assert warns == []

    def test_custom_threshold(self):
        v = _vault("v1", tvl=1000)
        # 60% of 100 = 60 USDC; TVL=1000; ratio=6%; threshold=5% → fires
        warns = capacity_warnings({"v1": 0.6}, 100, [v], warn_share=0.05)
        assert len(warns) == 1
        # At threshold=10% → no fire (6% < 10%)
        warns2 = capacity_warnings({"v1": 0.6}, 100, [v], warn_share=0.10)
        assert warns2 == []

    def test_realistic_ixs_500_usdc(self):
        """With 500 USDC budget and IXS TVL ~654, any IXS allocation fires."""
        v = _vault("ixs-rwa-bnb", tvl=654)
        # 40% of 500 = 200 USDC; 200/654 = 30.6% > 10%
        warns = capacity_warnings({"ixs-rwa-bnb": 0.4}, 500, [v])
        assert len(warns) == 1
        assert warns[0]["allocation_pct_of_tvl"] > 10


# ---------------------------------------------------------------------------
# Per-step token accounting
# ---------------------------------------------------------------------------

def _meta():
    return ModelMeta(model="fake", total_calls=0,
                     run_prompt_tokens=0, run_completion_tokens=0, run_total_tokens=0)


class TestPerStepAccounting:
    def test_single_parse_step(self):
        client = FakeServClient(reply='{"max_per_vault": 0.4}')
        meta = _meta()
        parse_policy("40% max", client, [0], 10, meta)
        assert len(meta.steps) == 1
        assert meta.steps[0].step == "policy_parse"
        assert meta.steps[0].calls == 1
        assert meta.steps[0].total_tokens == 15

    def test_repair_creates_separate_step(self):
        """Unknown key on first reply → repair retry → two distinct steps."""
        bad  = json.dumps({"completely_unknown_key_xyz": 0.4})
        good = json.dumps({"max_per_vault": 0.4})
        client = FakeServClient(replies=[bad, good])
        meta = _meta()
        parse_policy("40% max", client, [0], 10, meta)
        step_names = [s.step for s in meta.steps]
        assert "policy_parse" in step_names
        assert "policy_repair" in step_names

    def test_alias_does_not_trigger_repair(self):
        """Alias keys (in _KEY_ALIASES) must NOT cause a repair retry."""
        payload = json.dumps({"max_vault_allocation": 0.4})  # known alias
        client = FakeServClient(reply=payload)
        meta = _meta()
        parse_policy("40% max", client, [0], 10, meta)
        step_names = [s.step for s in meta.steps]
        assert "policy_repair" not in step_names
        assert meta.total_calls == 1

    def test_total_tokens_matches_sum_of_steps(self):
        client = FakeServClient(reply='{"max_per_vault": 0.5}')
        meta = _meta()
        parse_policy("50%", client, [0], 10, meta)
        step_total = sum(s.total_tokens for s in meta.steps)
        assert step_total == meta.run_total_tokens

    def test_proposal_step_labeled(self):
        from app.vaults.mock import build_mock_vaults
        from app.proposal import propose

        vaults = build_mock_vaults()
        valid = json.dumps({
            "allocation": {"mock-tbill": 0.5, "mock-bond": 0.3,
                           "mock-credit": 0.1, "mock-mm": 0.1},
            "rationale": "ok",
        })
        client = FakeServClient(reply=valid)
        meta = _meta()
        propose(Policy(), vaults, 100_000, {}, client, [0], 10, meta)
        step_names = [s.step for s in meta.steps]
        assert any("proposal_" in n for n in step_names)


# ---------------------------------------------------------------------------
# Alias vs truly-unknown-key repair
# ---------------------------------------------------------------------------

class TestAliasVsUnknownRepair:
    def _parse(self, reply, extra_reply=None):
        replies = [reply, extra_reply] if extra_reply else [reply]
        client = FakeServClient(replies=replies)
        meta = _meta()
        policy = parse_policy("test", client, [0], 10, meta)
        return policy, meta, client

    @pytest.mark.parametrize("alias_key,canonical,value", [
        ("max_vault_allocation",  "max_per_vault",  0.35),
        ("min_liquidity",         "min_liquid",      0.20),
        ("max_average_risk",      "max_avg_risk",    3.0),
        ("min_apy",               "min_avg_apy",     0.06),
        ("max_lockup_days",       "max_redemption_days", 7),
        ("max_vault_concentration","max_per_vault",  0.40),
    ])
    def test_alias_normalised_no_repair(self, alias_key, canonical, value):
        policy, meta, client = self._parse(json.dumps({alias_key: value}))
        assert getattr(policy, canonical) == value
        assert meta.total_calls == 1    # no repair retry

    def test_truly_unknown_triggers_repair(self):
        bad  = json.dumps({"completely_unknown_xyz_key": 0.5})
        good = json.dumps({"max_per_vault": 0.5})
        policy, meta, _ = self._parse(bad, good)
        assert policy.max_per_vault == 0.5
        assert meta.total_calls == 2    # repair retry fired

    def test_mixed_alias_and_canonical_no_repair(self):
        payload = json.dumps({
            "max_vault_allocation": 0.4,   # alias
            "max_avg_risk": 3.0,           # canonical
        })
        policy, meta, _ = self._parse(payload)
        assert policy.max_per_vault == 0.4
        assert policy.max_avg_risk == 3.0
        assert meta.total_calls == 1
