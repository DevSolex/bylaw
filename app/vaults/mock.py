"""
app/vaults/mock.py — simulated vault adapter for demos and tests.

All fields labeled `simulated`. Returns a fixed set of plausible vaults
covering the required archetypes: T-bill, bond, private credit, money market,
paused vault.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.models import Vault
from app.vaults.base import VaultAdapter

logger = logging.getLogger(__name__)

_SIM: dict[str, str] = {f: "simulated" for f in
    ["apy", "redemption_days", "risk", "paused", "available_liquidity_usdc"]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_mock_vaults() -> list[Vault]:
    now = _now()
    return [
        Vault(
            id="mock-tbill", name="T-Bill Yield Fund",
            chain="ethereum", asset="USDC",
            apy=0.052, redemption_days=0, settlement="sync",
            risk=1, paused=False, available_liquidity_usdc=None,
            data_sources=_SIM, fetched_at=now,
        ),
        Vault(
            id="mock-bond", name="Investment-Grade Bond Vault",
            chain="ethereum", asset="USDC",
            apy=0.079, redemption_days=3, settlement="sync",
            risk=3, paused=False, available_liquidity_usdc=None,
            data_sources=_SIM, fetched_at=now,
        ),
        Vault(
            id="mock-credit", name="Private Credit Pool",
            chain="ethereum", asset="USDC",
            apy=0.112, redemption_days=30, settlement="async",
            risk=4, paused=False, available_liquidity_usdc=None,
            data_sources=_SIM, fetched_at=now,
        ),
        Vault(
            id="mock-mm", name="Money Market Stablecoin Vault",
            chain="polygon", asset="USDC",
            apy=0.041, redemption_days=0, settlement="sync",
            risk=2, paused=False, available_liquidity_usdc=None,
            data_sources=_SIM, fetched_at=now,
        ),
        Vault(
            id="mock-paused", name="Paused Demo Vault",
            chain="ethereum", asset="USDC",
            apy=0.090, redemption_days=7, settlement="async",
            risk=3, paused=True, available_liquidity_usdc=None,
            data_sources=_SIM, fetched_at=now,
        ),
    ]


class MockAdapter(VaultAdapter):
    """Returns a fixed set of simulated vaults."""

    def list_vaults(self) -> list[Vault]:
        vaults = build_mock_vaults()
        logger.info("mock: returning %d simulated vaults", len(vaults))
        return vaults
