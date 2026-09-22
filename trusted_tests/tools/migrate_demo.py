"""Trusted migration tool: applies the candidate's additive migration to a copied demo database
with backup and verification evidence. Runs inside the worker container; prints one JSON line.

Usage: python migrate_demo.py /work/demo/demo.db
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/work/candidate")


def snapshot(db: Path) -> dict:
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(links)")]
    rows = conn.execute("SELECT code, target_url, created_at, click_count FROM links ORDER BY code").fetchall()
    conn.close()
    return {
        "columns": cols,
        "rows": len(rows),
        "click_sum": sum(r[3] for r in rows),
        "rows_hash": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
    }


def main() -> int:
    db = Path(sys.argv[1])
    backup = db.with_suffix(".db.bak")
    shutil.copyfile(db, backup)
    before = snapshot(db)
    backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    result = {"db": str(db), "backup": str(backup), "backup_sha256": backup_hash, "before": before}
    try:
        from app.main import create_app

        create_app(db_path=str(db), base_url="http://demo.local")
        after = snapshot(db)
        result["after"] = after
        ok = (
            after["rows"] == before["rows"]
            and after["click_sum"] == before["click_sum"]
            and after["rows_hash"] == before["rows_hash"]
            and set(after["columns"]) >= set(before["columns"])
        )
        result["additive_and_preserving"] = ok
        result["added_columns"] = sorted(set(after["columns"]) - set(before["columns"]))
        if not ok:
            shutil.copyfile(backup, db)
            result["restored_from_backup"] = True
            result["restored_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
        print(json.dumps(result))
        return 0 if ok else 2
    except Exception as e:  # noqa: BLE001 - report and restore
        shutil.copyfile(backup, db)
        result["error"] = repr(e)
        result["restored_from_backup"] = True
        print(json.dumps(result))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
