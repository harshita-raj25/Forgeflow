"""Scenario definitions: requirement text, stage, baseline lineage, target contract parts, seed data."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import SCENARIOS_DIR


@dataclass
class Scenario:
    id: str
    title: str
    stage: str  # A | B | C — selects which trusted product tests apply
    requirement: str
    brownfield: bool = False
    baseline_from: str | None = None
    contract_parts: list[str] = field(default_factory=list)
    seed_links: list[dict] = field(default_factory=list)
    suggested_answers: list[dict] = field(default_factory=list)
    revision_text: str | None = None

    def contract_text(self) -> str:
        parts = [(SCENARIOS_DIR / f"{p}.md").read_text() for p in self.contract_parts]
        return "\n\n".join(parts)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "stage": self.stage,
            "brownfield": self.brownfield,
            "baseline_from": self.baseline_from,
            "contract_parts": self.contract_parts,
            "seed_links": self.seed_links,
        }


def load_scenario(scenario_id: str) -> Scenario:
    p = SCENARIOS_DIR / f"{scenario_id}.json"
    if not p.exists():
        raise KeyError(f"unknown scenario {scenario_id!r}; available: {[q.stem for q in SCENARIOS_DIR.glob('*.json')]}")
    d = json.loads(p.read_text())
    return Scenario(**d)


def list_scenarios() -> list[Scenario]:
    return [load_scenario(p.stem) for p in sorted(SCENARIOS_DIR.glob("*.json"))]
