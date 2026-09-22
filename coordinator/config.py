"""Runtime configuration. Every limit is recorded per run so evidence shows the bounds in force."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
TRUSTED_TESTS_DIR = ROOT / "trusted_tests"
SCENARIOS_DIR = ROOT / "scenarios"
FIXTURES_DIR = ROOT / "fixtures"
PROMPTS_DIR = ROOT / "prompts"
SCHEMAS_DIR = ROOT / "schemas"
DEFAULT_DB = ROOT / "forgeflow.db"

# Paths generated code must never touch (relative to repo root). Enforced by policy.
PROTECTED_ROOTS = ("coordinator", "trusted_tests", "tests", "prompts", "schemas", "scenarios", "fixtures", "docker", "docs")


@dataclass(frozen=True)
class Budget:
    """Bounded recovery limits (PROJECT-SPEC §9)."""

    model_timeout_s: float = 90.0
    provider_retries: int = 2  # retries after the initial request for transient errors
    backoff_base_s: float = 1.0
    structured_output_corrections: int = 1
    repair_cycles: int = 2
    max_provider_calls: int = 30
    max_active_seconds: float = 20 * 60
    test_timeout_s: float = 120.0
    max_tasks: int = 20
    concurrency: int = 2
    max_files_per_edit: int = 40
    max_file_bytes: int = 200_000
    max_total_edit_bytes: int = 1_000_000
    max_output_bytes: int = 200_000

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Settings:
    db_path: Path = DEFAULT_DB
    runs_dir: Path = RUNS_DIR
    execution_mode: str = "fixture"  # "live" or "fixture"
    openai_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    worker_image: str = "forgeflow-worker:latest"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    human_label: str = "owner"
    budget: Budget = field(default_factory=Budget)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        mode = env.get("FORGEFLOW_MODE", "fixture").strip().lower()
        if mode not in ("live", "fixture"):
            raise ValueError(f"FORGEFLOW_MODE must be 'live' or 'fixture', got {mode!r}")
        budget = Budget(
            model_timeout_s=float(env.get("FORGEFLOW_MODEL_TIMEOUT_S", Budget.model_timeout_s)),
            provider_retries=int(env.get("FORGEFLOW_PROVIDER_RETRIES", Budget.provider_retries)),
            repair_cycles=int(env.get("FORGEFLOW_REPAIR_CYCLES", Budget.repair_cycles)),
            max_provider_calls=int(env.get("FORGEFLOW_MAX_PROVIDER_CALLS", Budget.max_provider_calls)),
            max_active_seconds=float(env.get("FORGEFLOW_MAX_ACTIVE_SECONDS", Budget.max_active_seconds)),
            test_timeout_s=float(env.get("FORGEFLOW_TEST_TIMEOUT_S", Budget.test_timeout_s)),
            concurrency=int(env.get("FORGEFLOW_CONCURRENCY", Budget.concurrency)),
        )
        return cls(
            db_path=Path(env.get("FORGEFLOW_DB", str(DEFAULT_DB))),
            runs_dir=Path(env.get("FORGEFLOW_RUNS_DIR", str(RUNS_DIR))),
            execution_mode=mode,
            openai_model=env.get("OPENAI_MODEL") or None,
            openai_base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            worker_image=env.get("FORGEFLOW_WORKER_IMAGE", "forgeflow-worker:latest"),
            bind_host=env.get("FORGEFLOW_HOST", "127.0.0.1"),
            bind_port=int(env.get("FORGEFLOW_PORT", "8000")),
            human_label=env.get("FORGEFLOW_HUMAN", "owner"),
            budget=budget,
        )

    def public_dict(self) -> dict:
        """Configuration safe to record in evidence. Never includes credentials."""
        return {
            "execution_mode": self.execution_mode,
            "openai_model": self.openai_model if self.execution_mode == "live" else None,
            "openai_base_url": self.openai_base_url if self.execution_mode == "live" else None,
            "worker_image": self.worker_image,
            "budget": self.budget.as_dict(),
        }
