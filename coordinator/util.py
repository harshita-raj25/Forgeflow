"""Small shared helpers: ids, hashing, time, secret redaction."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def new_id(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(obj) -> str:
    return sha256_text(canonical_json(obj))


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_tree(root: Path, ignore_dirs: tuple[str, ...] = ("__pycache__", ".pytest_cache", ".ruff_cache")) -> dict[str, str]:
    """Return {relative_path: sha256} for every regular file under root, sorted."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if any(part in ignore_dirs for part in p.parts):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        out[p.relative_to(root).as_posix()] = sha256_bytes(p.read_bytes())
    return out


def manifest_hash(files: dict[str, str]) -> str:
    return sha256_json(files)


# Secret redaction. Heuristic only; limitations documented in docs/limitations.md.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?([A-Za-z0-9._\-]{12,})"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS[:4]:
        out = pat.sub("[REDACTED]", out)
    out = _SECRET_PATTERNS[4].sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
    return out


def looks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
