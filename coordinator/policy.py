"""Policy engine: path validation, forbidden targets, edit limits, fixed command allowlist,
untrusted-text handling. Model output proposes; only this module decides what is permitted.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import PROTECTED_ROOTS, Budget


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# Fixed validation commands. Model output can only reference these names.
FIXED_COMMANDS: dict[str, list[str]] = {
    "lint": ["ruff", "check", "--no-cache", "--quiet", "/work/candidate"],
    "test": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout=100", "/work/trusted_tests"],
    "migrate": ["python", "/work/trusted_tests/tools/migrate_demo.py", "/work/demo/demo.db"],
    "isolation_check": ["python", "/work/trusted_tests/tools/check_isolation.py"],
}

# Paths inside a candidate workspace that generated code may write.
CANDIDATE_ALLOWED_PREFIXES = ("app/", "tests/")
CANDIDATE_ALLOWED_FILES = ("README.md", "requirements.txt", "pyproject.toml", "API.md")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._\-]+$")

# Instruction-like phrases in repository text are data, never commands. We flag them for the event log.
_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore (all|any|previous|prior) (instructions|rules|policy)|write to /|outside (the )?workspace|"
    r"\.\./\.\./|/etc/passwd|trusted_tests|coordinator/|disable (the )?(policy|sandbox)|approve (this|the) release)"
)


@dataclass(frozen=True)
class EditProposal:
    path: str
    content: str
    op: str = "write"  # write | delete


def normalize_candidate_path(raw: str) -> str:
    """Validate a model-proposed relative path. Raises PolicyViolation."""
    if not isinstance(raw, str) or not raw.strip():
        raise PolicyViolation("empty_path", "path is empty")
    if "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise PolicyViolation("control_char", f"path contains control characters: {raw!r}")
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        raise PolicyViolation("absolute_path", f"absolute paths are not allowed: {raw!r}")
    if "\\" in raw:
        raise PolicyViolation("backslash", f"backslashes are not allowed: {raw!r}")
    p = PurePosixPath(raw)
    parts = p.parts
    if any(part in ("..", ".") for part in parts):
        raise PolicyViolation("traversal", f"path traversal is not allowed: {raw!r}")
    if any(not _SAFE_SEGMENT.match(part) for part in parts):
        raise PolicyViolation("unsafe_segment", f"path segment not allowed: {raw!r}")
    norm = p.as_posix()
    # Candidate paths are relative to the disposable candidate workspace, not the repository root, so a
    # segment happening to match a repo-root protected name (e.g. "tests/") is not itself a violation here;
    # CANDIDATE_ALLOWED_PREFIXES below is the real allowlist, and resolve_inside() enforces containment.
    if not (norm.startswith(CANDIDATE_ALLOWED_PREFIXES) or norm in CANDIDATE_ALLOWED_FILES):
        raise PolicyViolation("outside_allowed", f"path is outside allowed candidate areas {CANDIDATE_ALLOWED_PREFIXES}: {raw!r}")
    if norm.endswith((".sh", ".so", ".dylib", ".exe")) or "/." in "/" + norm:
        raise PolicyViolation("forbidden_type", f"hidden or executable files are not allowed: {raw!r}")
    return norm


def check_allowed_paths(norm: str, allowed_globs: tuple[str, ...]) -> None:
    """Task-level allowed paths from the approved plan: exact filenames, or a directory prefix ending in
    `/*` (or bare `*`). An entry with no wildcard authorizes only that exact path, never a sibling file
    with an appended suffix (`app/main.py` must not also authorize `app/main.py.bak`)."""
    if not allowed_globs:
        return
    from fnmatch import fnmatch

    for g in allowed_globs:
        if fnmatch(norm, g):
            return
        if g.endswith("*"):
            prefix = g[:-1]
            # A directory-prefix glob like "app/*" authorizes anything under app/; require the boundary
            # to fall on a path separator (or the glob to already end in one) so "app/m*" doesn't also
            # match "appendix/x" and "app*" doesn't also match "application/x" one level too broadly.
            if norm.startswith(prefix) and (prefix.endswith("/") or norm[len(prefix) :].startswith("/") or prefix == ""):
                return
        elif norm == g:
            return
    raise PolicyViolation("task_scope", f"path {norm!r} is outside the task's approved allowed_paths {list(allowed_globs)}")


def validate_edits(edits: list[dict], budget: Budget, allowed_globs: tuple[str, ...] = ()) -> list[EditProposal]:
    if not isinstance(edits, list):
        raise PolicyViolation("bad_edits", "edits must be a list")
    if len(edits) > budget.max_files_per_edit:
        raise PolicyViolation("too_many_files", f"{len(edits)} files exceed limit {budget.max_files_per_edit}")
    out: list[EditProposal] = []
    total = 0
    seen: set[str] = set()
    for e in edits:
        op = e.get("op", "write")
        if op not in ("write", "delete"):
            raise PolicyViolation("bad_op", f"unknown edit op {op!r}")
        norm = normalize_candidate_path(e.get("path", ""))
        check_allowed_paths(norm, allowed_globs)
        if norm in seen:
            raise PolicyViolation("duplicate_path", f"path {norm} appears twice in one edit set")
        seen.add(norm)
        content = e.get("content", "") or ""
        if op == "write":
            if not isinstance(content, str):
                raise PolicyViolation("bad_content", f"content for {norm} must be a string")
            b = len(content.encode("utf-8"))
            if b > budget.max_file_bytes:
                raise PolicyViolation("file_too_large", f"{norm} is {b} bytes, limit {budget.max_file_bytes}")
            total += b
            if total > budget.max_total_edit_bytes:
                raise PolicyViolation("edit_set_too_large", f"edit set exceeds {budget.max_total_edit_bytes} bytes")
        out.append(EditProposal(norm, content, op))
    return out


def resolve_inside(workspace: Path, norm: str) -> Path:
    """Resolve a validated relative path and ensure it stays inside the workspace even via symlinks."""
    ws = workspace.resolve()
    target = ws / norm
    # Check every existing ancestor for symlink escape.
    cur = target
    while True:
        if cur.exists() or cur.is_symlink():
            if cur.is_symlink():
                raise PolicyViolation("symlink", f"{cur.relative_to(ws)} is a symlink; refusing to write through it")
            real = cur.resolve()
            if real != ws and ws not in real.parents:
                raise PolicyViolation("escape", f"{norm} resolves outside the workspace")
            break
        if cur == ws or cur.parent == cur:
            break
        cur = cur.parent
    final = target.resolve() if target.exists() else target.parent.resolve() / target.name
    if final != ws and ws not in final.parents:
        raise PolicyViolation("escape", f"{norm} resolves outside the workspace")
    return final


def write_text_no_symlink(path: Path, content: str) -> None:
    """Write text to a host path that a container may have had read-write access to (e.g. the demo dir
    bind-mounted for migration), refusing to follow an existing symlink. A container's own process can
    plant a symlink inside a bind-mounted directory pointing anywhere on the host filesystem -- the mount
    boundary does not constrain what a symlink *target string* can say, only what the container's own
    processes can directly read/write. `O_NOFOLLOW` makes the refusal atomic at the kernel level (no
    check-then-open race): if the path is a symlink, this raises before anything is written, rather than
    silently writing through it to whatever it points at (fifth review round finding)."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        raise PolicyViolation("symlink", f"refusing to write {path}: {e}") from e
    with os.fdopen(fd, "w") as f:
        f.write(content)


def fixed_command(name: str) -> list[str]:
    if name not in FIXED_COMMANDS:
        raise PolicyViolation("command_not_allowed", f"only fixed commands {sorted(FIXED_COMMANDS)} may run; got {name!r}")
    return list(FIXED_COMMANDS[name])


def scan_untrusted_text(text: str) -> list[str]:
    """Return instruction-like markers found in untrusted repository or requirement text (for logging)."""
    return sorted({m.group(0) for m in _INJECTION_MARKERS.finditer(text or "")})


def is_trusted_path(path: Path, root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return rel.parts and rel.parts[0] in PROTECTED_ROOTS


def worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for worker execution: explicit allowlist only. Never inherits provider credentials."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
    }
    if extra:
        for k, v in extra.items():
            if k.upper().startswith(("OPENAI", "ANTHROPIC", "AWS", "AZURE", "GOOGLE")) or "KEY" in k.upper() or "TOKEN" in k.upper():
                raise PolicyViolation("credential_env", f"refusing to pass {k} into a worker")
            env[k] = v
    for k in os.environ:
        assert k not in env or not k.startswith("OPENAI"), "provider credentials must never reach workers"
    return env
