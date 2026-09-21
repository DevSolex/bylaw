"""
app/vaults/curated.py — reads config/vaults.yaml and returns Vault objects.

Fields sourced from this file are labeled `curated`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.models import SourceLabel, Vault
from app.vaults.base import VaultAdapter

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent.parent.parent / "config" / "vaults.yaml"

_SETTLEMENT_MAP = {"sync": "sync", "async": "async", "async-erc7540": "async"}


def _parse_vault(raw: dict, fetched_at: datetime) -> Vault | None:
    try:
        data_sources: dict[str, SourceLabel] = raw.get("data_sources", {})
        return Vault(
            id=raw["id"],
            name=raw["name"],
            chain=raw["chain"],
            asset=raw.get("asset", "USDC"),
            apy=raw.get("apy"),
            apy_label=raw.get("apy_label"),
            redemption_days=raw.get("redemption_days"),
            settlement=_SETTLEMENT_MAP.get(raw.get("settlement", ""), None),
            risk=raw["risk"],
            paused=raw.get("paused"),
            total_assets_usdc=raw.get("total_assets_usdc"),
            price_per_share=raw.get("price_per_share"),
            as_of=raw.get("as_of"),
            data_sources=data_sources,
            fetched_at=fetched_at,
        )
    except Exception as exc:
        logger.warning("curated: skipping vault %r: %s", raw.get("id"), exc)
        return None


class CuratedAdapter(VaultAdapter):
    """Reads vaults from config/vaults.yaml. All fields labeled curated."""

    def __init__(self, yaml_path: Path = _YAML_PATH):
        self._path = yaml_path

    def list_vaults(self) -> list[Vault]:
        now = datetime.now(timezone.utc)
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("curated: failed to load %s: %s", self._path, exc)
            return []

        vaults: list[Vault] = []
        for entry in raw.get("vaults", []):
            v = _parse_vault(entry, now)
            if v:
                vaults.append(v)
        logger.info("curated: loaded %d vaults", len(vaults))
        return vaults
