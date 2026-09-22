"""Trusted acceptance tests for the generated URL shortener.

These tests are owned by the evaluator, mounted read-only into the worker container, and are never
writable by runtime agents. They exercise the contract in scenarios/contract_*.md by importing the
candidate's `app.main.create_app` factory. FORGEFLOW_STAGE selects which stages apply (A, B, C).
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

CANDIDATE = Path(os.environ.get("FORGEFLOW_CANDIDATE", "/work/candidate"))
STAGE = os.environ.get("FORGEFLOW_STAGE", "A").upper()
STAGE_ORDER = {"A": 1, "B": 2, "C": 3}

if str(CANDIDATE) not in sys.path:
    sys.path.insert(0, str(CANDIDATE))


def stage_at_least(stage: str) -> bool:
    return STAGE_ORDER.get(STAGE, 0) >= STAGE_ORDER[stage]


def pytest_collection_modifyitems(config, items):
    for item in items:
        for stage in ("B", "C"):
            if item.get_closest_marker(f"stage_{stage.lower()}") and not stage_at_least(stage):
                item.add_marker(pytest.mark.skip(reason=f"stage {stage} tests not selected (FORGEFLOW_STAGE={STAGE})"))


def pytest_configure(config):
    config.addinivalue_line("markers", "stage_b: custom alias tests")
    config.addinivalue_line("markers", "stage_c: expiry tests")


class Clock:
    def __init__(self, at: datetime | None = None):
        self.at = at or datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def app_module():
    if "app.main" in sys.modules:
        return importlib.reload(sys.modules["app.main"])
    return importlib.import_module("app.main")


@pytest.fixture
def make_app(app_module, tmp_path):
    def _make(db_name: str = "links.db", **kw):
        return app_module.create_app(db_path=str(tmp_path / db_name), base_url="http://short.test", **kw)

    return _make


@pytest.fixture
def client(make_app):
    from fastapi.testclient import TestClient

    with TestClient(make_app(), follow_redirects=False) as c:
        yield c
