"""Live OpenAI adapter (Chat Completions, strict JSON-schema structured output).

Credentials are read from the environment by the coordinator process only. They are never written to
state, events, artifacts, exports, or worker containers. Responses are validated locally against the
role schema; schema conformance does not establish semantic correctness, so every action is still gated.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx

from ..util import redact
from .base import AdapterError, InvalidModelOutput, ModelRequest, ModelResponse, validate_output

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAIAdapter:
    execution_mode = "live"

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 90.0,
        retries: int = 2,
        backoff_base_s: float = 1.0,
        api_key: str | None = None,
        reasoning_effort: str | None = "medium",
    ):
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key:
            raise AdapterError("OPENAI_API_KEY is not set; live mode cannot start", category="missing_credentials")
        model = model or os.environ.get("OPENAI_MODEL")
        if not model:
            raise AdapterError("OPENAI_MODEL is not set; refusing to invent a model name", category="missing_model")
        self._key = key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_base_s = backoff_base_s
        self.reasoning_effort = reasoning_effort
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=15.0))

    def __repr__(self) -> str:  # never leak the key through repr/logging
        return f"OpenAIAdapter(model={self.model!r}, base_url={self.base_url!r})"

    def complete(self, req: ModelRequest) -> ModelResponse:
        request_id = f"ff-{uuid.uuid4().hex[:16]}"
        messages = [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}]
        if req.correction_of is not None:
            messages.append({"role": "assistant", "content": req.correction_of})
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous output did not satisfy the required JSON schema or content rules. "
                    "Return a corrected JSON object that satisfies the schema exactly.",
                }
            )
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "strict": True, "schema": req.schema},
            },
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Request-Id": request_id}
        attempt = 0
        started = time.monotonic()
        last_err: str = ""
        while True:
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"transport: {type(e).__name__}: {redact(str(e))}"
                resp = None
            if resp is not None and resp.status_code == 200:
                payload = resp.json()
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise InvalidModelOutput("model output truncated (finish_reason=length)", raw=choice["message"].get("content") or "")
                if choice["message"].get("refusal"):
                    raise InvalidModelOutput(f"model refused: {choice['message']['refusal']}")
                raw = choice["message"].get("content") or ""
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise InvalidModelOutput(f"model output is not JSON: {e}", raw=raw) from e
                validate_output(data, req.schema)
                usage = payload.get("usage") or {}
                return ModelResponse(
                    data=data,
                    raw=raw,
                    request_id=request_id,
                    model=payload.get("model", self.model),
                    usage={k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    retries=attempt,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    execution_mode="live",
                )
            if resp is not None:
                text = redact(resp.text[:500])
                last_err = f"HTTP {resp.status_code}: {text}"
                if resp.status_code not in TRANSIENT_STATUS:
                    raise AdapterError(f"provider error (non-transient) request_id={request_id}: {last_err}")
            if attempt >= self.retries:
                raise AdapterError(f"provider error after {attempt} retries request_id={request_id}: {last_err}", transient=True)
            attempt += 1
            time.sleep(min(self.backoff_base_s * (2 ** (attempt - 1)), 20.0))
