"""
Configuration — reads environment variables.

Uses pydantic-settings so the same object works in tests (override via env)
and in Docker (env_file from docker-compose.yml).
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SERV / OpenServ
    serv_api_key: str = "offline"
    serv_base_url: str = "https://inference-api.openserv.ai/v1"
    serv_model: str = "serv-model-placeholder"  # see docs/OPEN_QUESTIONS.md OQ-5

    # Maximum total SERV calls permitted within a single Run.
    # Covers: 1 policy parse + up to 3 proposal attempts + 1 repair = 5 by default.
    # Raise if you need more headroom; lower to protect credit spend.
    serv_max_calls_per_run: int = 10

    # Capacity warning threshold (0.0-1.0).
    # Warn when an allocation exceeds this fraction of a vault's total assets.
    capacity_warn_share: float = 0.10

    # Data mode
    data_mode: str = "mixed"   # live | mixed | simulated — default mixed shows live IXS vault

    # Offline demo stub — default True so tests never need a real API key.
    # Set OFFLINE_DEMO=0 in .env for live usage.
    offline_demo: bool = True

    # IXS adapter
    ixs_mcp_url: str = ""
    ixs_api_base_url: str = ""
    ixs_vault_id: str = ""
    ixs_rpc_url: str = ""

    # Logging
    log_level: str = "INFO"

    @field_validator("data_mode")
    @classmethod
    def validate_data_mode(cls, v: str) -> str:
        allowed = {"live", "mixed", "simulated"}
        if v not in allowed:
            raise ValueError(f"DATA_MODE must be one of {allowed}; got {v!r}")
        return v


settings = Settings()
