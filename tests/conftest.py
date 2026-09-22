"""
tests/conftest.py — shared pytest fixtures.

CRITICAL SAFETY RULE: tests must never write to the real ./data/runs directory.
Every test gets its own empty temporary runs directory. The real data path is
blocked at session start and checked before every test.
"""
import tempfile
from pathlib import Path

import pytest

_REAL_RUNS_DIR = Path("/app/data/runs")
_SESSION_SAFE_DIR: Path | None = None


def pytest_sessionstart(session):
    """Redirect _RUNS_DIR away from the real path before any test runs."""
    import app.runs as _runs_module
    global _SESSION_SAFE_DIR
    _SESSION_SAFE_DIR = Path(tempfile.mkdtemp(prefix="bylaw_session_"))
    _runs_module._RUNS_DIR = _SESSION_SAFE_DIR


def pytest_runtest_setup(item):
    import app.runs as _runs_module
    assert str(_runs_module._RUNS_DIR) != str(_REAL_RUNS_DIR), (
        "SAFETY: test suite would write to the real audit log. Check conftest.py."
    )


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path):
    """Each test gets its own empty runs directory — no cross-test leakage."""
    import app.runs as _runs_module
    test_dir = tmp_path / "runs"
    test_dir.mkdir()
    _runs_module._RUNS_DIR = test_dir
    yield test_dir
    # Restore to session-safe dir (never to the real path)
    _runs_module._RUNS_DIR = _SESSION_SAFE_DIR
