"""Candidate workspaces: disposable copies of a baseline, validated edits, manifests, diffs, snapshots, restore."""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

from .policy import EditProposal, PolicyViolation, resolve_inside
from .util import hash_tree, iso, manifest_hash, sha256_bytes

IGNORE = ("__pycache__", ".pytest_cache", ".ruff_cache", "*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.pyc")


class Workspace:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.baseline_dir = self.run_dir / "baseline"
        self.candidate_dir = self.run_dir / "candidate"
        self.snapshots_dir = self.run_dir / "snapshots"
        self.artifacts_dir = self.run_dir / "artifacts"
        for d in (self.run_dir, self.snapshots_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- baseline / candidate lifecycle ---------------------------------
    def init_baseline(self, source: Path | None) -> str:
        """Copy a baseline (approved snapshot) or create an empty one. Returns baseline hash."""
        if self.baseline_dir.exists():
            shutil.rmtree(self.baseline_dir)
        if source is not None and source.exists():
            shutil.copytree(source, self.baseline_dir, ignore=shutil.ignore_patterns(*IGNORE), symlinks=False)
        else:
            self.baseline_dir.mkdir(parents=True)
        return self.baseline_hash()

    def baseline_hash(self) -> str:
        return manifest_hash(hash_tree(self.baseline_dir))

    def reset_candidate_from_baseline(self) -> str:
        if self.candidate_dir.exists():
            shutil.rmtree(self.candidate_dir)
        shutil.copytree(self.baseline_dir, self.candidate_dir, ignore=shutil.ignore_patterns(*IGNORE), symlinks=False)
        return self.candidate_hash()

    def candidate_manifest(self) -> dict[str, str]:
        return hash_tree(self.candidate_dir)

    def candidate_hash(self) -> str:
        return manifest_hash(self.candidate_manifest())

    # ---- reading (for model context) -----------------------------------
    def list_files(self, root: Path | None = None) -> list[str]:
        root = root or self.candidate_dir
        return sorted(hash_tree(root).keys())

    def read_files(self, root: Path | None = None, max_bytes: int = 60_000) -> dict[str, str]:
        root = root or self.candidate_dir
        out: dict[str, str] = {}
        for rel in self.list_files(root):
            p = root / rel
            data = p.read_bytes()
            if len(data) > max_bytes:
                out[rel] = f"[omitted: {len(data)} bytes]"
                continue
            try:
                out[rel] = data.decode("utf-8")
            except UnicodeDecodeError:
                out[rel] = f"[binary: {len(data)} bytes]"
        return out

    # ---- writes (only through validated proposals) ----------------------
    def apply_edits(self, edits: list[EditProposal]) -> list[dict]:
        """Apply validated edits to the candidate. Returns per-file records with pre/post hashes."""
        records = []
        # Validate all targets before touching anything (all-or-nothing on policy).
        targets = [(e, resolve_inside(self.candidate_dir, e.path)) for e in edits]
        for e, target in targets:
            pre = sha256_bytes(target.read_bytes()) if target.exists() else None
            if e.op == "delete":
                if target.exists():
                    target.unlink()
                post = None
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(e.content, encoding="utf-8")
                post = sha256_bytes(target.read_bytes())
            records.append({"path": e.path, "op": e.op, "pre_hash": pre, "post_hash": post})
        return records

    # ---- diffs -----------------------------------------------------------
    def diff(self, old_root: Path | None = None, new_root: Path | None = None) -> str:
        old_root = old_root or self.baseline_dir
        new_root = new_root or self.candidate_dir
        old_files = hash_tree(old_root)
        new_files = hash_tree(new_root)
        chunks: list[str] = []
        for rel in sorted(set(old_files) | set(new_files)):
            a = (old_root / rel).read_text(errors="replace").splitlines(keepends=True) if rel in old_files else []
            b = (new_root / rel).read_text(errors="replace").splitlines(keepends=True) if rel in new_files else []
            if a == b:
                continue
            chunks.append("".join(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}")))
        return "".join(chunks)

    # ---- snapshots / restore --------------------------------------------
    def snapshot(self, label: str) -> tuple[Path, str]:
        dest = self.snapshots_dir / label
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.candidate_dir, dest, ignore=shutil.ignore_patterns(*IGNORE), symlinks=False)
        h = manifest_hash(hash_tree(dest))
        (dest.parent / f"{label}.manifest.json").write_text(
            json.dumps({"label": label, "hash": h, "files": hash_tree(dest), "created_at": iso()}, indent=2)
        )
        return dest, h

    def restore(self, snapshot_label: str) -> dict:
        """Restore candidate from a snapshot. Records failed-state hash, backup hash and restored hash."""
        src = self.snapshots_dir / snapshot_label
        if not src.exists():
            raise PolicyViolation("no_snapshot", f"snapshot {snapshot_label} does not exist")
        failed_hash = self.candidate_hash()
        failed_copy = self.snapshots_dir / f"failed-{iso().replace(':', '')}"
        shutil.copytree(self.candidate_dir, failed_copy, ignore=shutil.ignore_patterns(*IGNORE), symlinks=False)
        backup_hash = manifest_hash(hash_tree(src))
        shutil.rmtree(self.candidate_dir)
        shutil.copytree(src, self.candidate_dir, ignore=shutil.ignore_patterns(*IGNORE), symlinks=False)
        restored = self.candidate_hash()
        return {
            "snapshot": snapshot_label,
            "failed_hash": failed_hash,
            "failed_copy": failed_copy.name,
            "backup_hash": backup_hash,
            "restored_hash": restored,
            "restore_ok": restored == backup_hash,
        }

    # ---- artifacts -------------------------------------------------------
    def write_artifact(self, rel: str, content: str | bytes) -> tuple[Path, str]:
        p = self.artifacts_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        p.write_bytes(data)
        return p, sha256_bytes(data)
