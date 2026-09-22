"""Wires settings, store, adapter, runner and coordinator into one object graph."""

from __future__ import annotations

from .adapters import FixtureAdapter, OpenAIAdapter
from .config import Settings
from .roles import ensure_prompt_files_exist, write_default_schemas
from .runner import DockerRunner
from .scheduler import Coordinator
from .store import Store


def build_coordinator(settings: Settings | None = None) -> Coordinator:
    settings = settings or Settings.from_env()
    write_default_schemas()
    missing = ensure_prompt_files_exist()
    if missing:
        raise RuntimeError(f"missing prompt/schema files: {missing}")
    store = Store(settings.db_path)
    if settings.execution_mode == "live":
        adapter = OpenAIAdapter(
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.budget.model_timeout_s,
            retries=settings.budget.provider_retries,
        )
    else:
        adapter = FixtureAdapter()
    runner = DockerRunner(settings.worker_image, timeout_s=settings.budget.test_timeout_s)
    return Coordinator(settings, store, adapter, runner)
