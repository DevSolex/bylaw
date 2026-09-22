"""
app/runs.py — audit log store.

Runs are saved as JSON files in ./data/runs/<id>.json.
The ./data directory is a Docker volume so runs survive restarts.
Tests patch _RUNS_DIR via tests/conftest.py — never written to the real path.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models import Run

logger = logging.getLogger(__name__)

_RUNS_DIR = Path("/app/data/runs")


def _ensure_dir() -> Path:
    try:
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        import tempfile
        fallback = Path(tempfile.gettempdir()) / "bylaw_runs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return _RUNS_DIR


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def save_run(run: Run) -> None:
    d = _ensure_dir()
    path = d / f"{run.id}.json"
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    logger.debug("run saved: %s", path)


def load_run(run_id: str) -> Run | None:
    path = _ensure_dir() / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return Run.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("failed to load run %s: %s", run_id, exc)
        return None


def list_runs(limit: int = 200) -> list[Run]:
    d = _ensure_dir()
    runs: list[Run] = []
    for p in d.glob("*.json"):
        try:
            runs.append(Run.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("skipping corrupt run file %s: %s", p.name, exc)
    # Sort by created_at descending — authoritative, not mtime
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs[:limit]


def run_status(run: Run) -> str:
    """Human-readable status for the history list."""
    if run.decision in ("approved", "rejected"):
        return run.decision
    if run.final is None:
        return "pending"
    if run.final.verified:
        return "pending"          # verified but awaiting user decision
    return "failed verification"
