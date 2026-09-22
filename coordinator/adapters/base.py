"""Model adapter contract. Adapters return validated JSON; the coordinator decides what to do with it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import jsonschema


class AdapterError(Exception):
    """Provider failure after bounded retries. Never silently substituted."""

    def __init__(self, message: str, category: str = "provider_error", transient: bool = False):
        super().__init__(message)
        self.category = category
        self.transient = transient


class InvalidModelOutput(AdapterError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message, category="invalid_output")
        self.raw = raw


class BudgetExhausted(AdapterError):
    def __init__(self, message: str):
        super().__init__(message, category="budget_exhausted")


@dataclass
class ModelRequest:
    role: str
    system: str
    user: str
    schema: dict
    schema_name: str
    scenario: str = ""
    task_id: str = ""
    attempt: int = 1
    correction_of: str | None = None  # raw invalid output when asking for a correction


@dataclass
class ModelResponse:
    data: dict
    raw: str
    request_id: str
    model: str
    usage: dict = field(default_factory=dict)
    retries: int = 0
    latency_ms: int = 0
    execution_mode: str = "fixture"


class ModelAdapter(Protocol):
    execution_mode: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...


def validate_output(data: object, schema: dict) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise InvalidModelOutput(f"output failed schema validation: {e.message} at {list(e.absolute_path)}") from e
