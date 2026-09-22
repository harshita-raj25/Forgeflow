"""Deterministic fixture adapter for offline engine tests and rehearsal.

Everything produced through this adapter is labeled execution_mode=fixture. It never stands in for a
failed live call: the coordinator constructs exactly one adapter per run and records its mode.
Fixture files live in fixtures/<scenario>/<role>[.<task_id>][.rev<N>].json and are plain JSON documents
matching the role schema. A missing fixture is an error, not a silent fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import FIXTURES_DIR
from .base import AdapterError, ModelRequest, ModelResponse, validate_output


class FixtureAdapter:
    execution_mode = "fixture"

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR, overrides: dict[str, dict] | None = None):
        self.fixtures_dir = Path(fixtures_dir)
        self.overrides = overrides or {}  # key -> data (tests inject role outputs directly)
        self.calls: list[ModelRequest] = []
        self.fail_next: list[Exception] = []  # tests push exceptions to simulate provider failures

    def _candidates(self, req: ModelRequest, revision: int | None) -> list[str]:
        keys = []
        base = f"{req.scenario}/{req.role}"
        if req.task_id:
            if revision:
                keys.append(f"{base}.{req.task_id}.rev{revision}")
            keys.append(f"{base}.{req.task_id}")
        if revision:
            keys.append(f"{base}.rev{revision}")
        keys.append(base)
        return keys

    def complete(self, req: ModelRequest, revision: int | None = None) -> ModelResponse:
        self.calls.append(req)
        if self.fail_next:
            raise self.fail_next.pop(0)
        data = None
        used = None
        for key in self._candidates(req, revision):
            if key in self.overrides:
                data, used = self.overrides[key], f"override:{key}"
                break
            p = self.fixtures_dir / f"{key}.json"
            if p.exists():
                data, used = json.loads(p.read_text()), str(p.relative_to(self.fixtures_dir))
                break
        if data is None:
            raise AdapterError(
                f"no fixture for scenario={req.scenario!r} role={req.role!r} task={req.task_id!r} (looked for {self._candidates(req, revision)})",
                category="missing_fixture",
            )
        if isinstance(data, dict) and "_fixture_note" in data:
            data = {k: v for k, v in data.items() if k != "_fixture_note"}
        validate_output(data, req.schema)
        raw = json.dumps(data)
        return ModelResponse(data=data, raw=raw, request_id=f"fixture:{used}", model="fixture", execution_mode="fixture")
