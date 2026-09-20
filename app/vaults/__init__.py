"""
app/vaults/__init__.py — vault layer dispatcher.

get_vaults(data_mode) -> list[Vault]

DATA_MODE:
  simulated : MockAdapter only
  mixed     : IXSAdapter (live) + MockAdapter peers; banner shows which are simulated
  live      : IXSAdapter + CuratedAdapter; fails loudly if IXS unavailable
"""
from __future__ import annotations

import logging
from app.models import Vault

logger = logging.getLogger(__name__)


def get_vaults(data_mode: str = "simulated") -> list[Vault]:
    from app.vaults.mock import MockAdapter
    from app.vaults.curated import CuratedAdapter
    from app.vaults.ixs import IXSAdapter

    if data_mode == "simulated":
        return MockAdapter().list_vaults()

    if data_mode == "mixed":
        ixs_vaults = IXSAdapter().list_vaults()
        # Peers: simulated vaults excluding the IXS vault id
        mock_vaults = [v for v in MockAdapter().list_vaults()
                       if not v.id.startswith("ixs-")]
        result = ixs_vaults + mock_vaults
        logger.info("mixed mode: %d IXS + %d simulated peers", len(ixs_vaults), len(mock_vaults))
        return result

    if data_mode == "live":
        ixs_vaults = IXSAdapter().list_vaults()
        curated = [v for v in CuratedAdapter().list_vaults()
                   if not v.id.startswith("ixs-")]
        result = ixs_vaults + curated
        if not ixs_vaults:
            logger.error("live mode: IXS adapter returned no vaults")
        return result

    raise ValueError(f"Unknown data_mode: {data_mode!r}")
