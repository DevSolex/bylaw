"""
Bylaw — FastAPI application entry point.

Only /healthz is wired up in M0. Remaining routes are added in M2–M4.
"""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Bylaw",
    description="Policy-driven allocator for tokenized RWA yield vaults.",
    version="0.1.0",
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    """Liveness probe — returns 200 {status: ok}."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static UI (added in M4; the directory must exist for the mount to succeed)
# ---------------------------------------------------------------------------

import os  # noqa: E402

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)

app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
