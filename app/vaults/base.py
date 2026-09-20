"""
app/vaults/base.py — adapter interface.

All adapters implement VaultAdapter.list_vaults() -> list[Vault].
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from app.models import Vault


class VaultAdapter(ABC):
    @abstractmethod
    def list_vaults(self) -> list[Vault]:
        """Return current vault snapshots. Never raise; return [] on failure."""
        ...
