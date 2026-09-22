from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator.adapters.fixture import FixtureAdapter
from coordinator.config import Settings
from coordinator.runner import DockerRunner
from coordinator.scheduler import Coordinator
from coordinator.store import Store


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An isolated ForgeFlow coordinator: its own SQLite db, runs dir, and fixture scenarios/fixtures
    copied in so tests never touch the real project state."""
    root = tmp_path
    (root / "runs").mkdir()
    shutil.copytree(Path(__file__).parent.parent / "scenarios", root / "scenarios")
    shutil.copytree(Path(__file__).parent.parent / "fixtures", root / "fixtures", ignore=shutil.ignore_patterns("build_fixtures.py"))
    monkeypatch.setattr("coordinator.config.SCENARIOS_DIR", root / "scenarios")
    monkeypatch.setattr("coordinator.scenarios.SCENARIOS_DIR", root / "scenarios")
    settings = Settings(db_path=root / "forgeflow.db", runs_dir=root / "runs", execution_mode="fixture")
    store = Store(settings.db_path)
    adapter = FixtureAdapter(fixtures_dir=root / "fixtures")
    runner = DockerRunner("forgeflow-worker:latest", trusted_tests_dir=Path(__file__).parent.parent / "trusted_tests")
    coord = Coordinator(settings, store, adapter, runner)
    yield coord


def approve_all(coord, run_id, stop_before=None):
    while True:
        v = coord.snapshot_view(run_id)
        pend = next((a for a in v["approvals"] if a["status"] == "pending"), None)
        if not pend or pend["scope"] == stop_before:
            return pend
        coord.decide_approval(run_id, pend["id"], True, f"{pend['scope']} approved")
        coord.resume(run_id)
