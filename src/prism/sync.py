"""Notes -> database: read each note's status & track moved notes. Only reads notes."""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from prism import db
from prism.export import read_frontmatter

STATUSES = ("new", "picked", "dropped")
_ID = re.compile(r"^prism_id:\s*(\S+)\s*$", re.MULTILINE)


@dataclass
class SyncReport:
    changed: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (reel, from, to)
    moved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # note not found in vault_dir
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (reel, odd status)


def _index_vault(vault: Path) -> dict[str, Path]:
    """prism_id -> file, from the head of each .md (hidden dirs like .obsidian skipped)."""
    found = {}
    for p in vault.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(vault).parts):
            continue
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        if head.startswith("---") and (m := _ID.search(head)):
            found.setdefault(m.group(1), p)
    return found


def run(conn: sqlite3.Connection, vault: Path) -> SyncReport:
    r = SyncReport()
    index = None  # scanning the vault is costly, only when a note left its note_path

    rows = conn.execute(
        "SELECT id, note_path, user_status FROM reels WHERE note_path IS NOT NULL"
        " ORDER BY id"
    ).fetchall()
    for reel in rows:
        path = Path(reel["note_path"])
        if not path.is_file():
            index = _index_vault(vault) if index is None else index
            path = index.get(reel["id"])
            if path is None:
                r.missing.append(reel["id"])
                continue
            with conn:
                conn.execute(
                    "UPDATE reels SET note_path = ? WHERE id = ?",
                    (str(path), reel["id"]),
                )
            r.moved.append(reel["id"])

        status = read_frontmatter(
            path.read_text(encoding="utf-8", errors="replace")
        ).get("status")
        if status not in STATUSES:
            r.invalid.append((reel["id"], str(status)))
        elif status != reel["user_status"]:
            with conn:
                conn.execute(
                    "UPDATE reels SET user_status = ?, updated_at = datetime('now')"
                    " WHERE id = ?",
                    (status, reel["id"]),
                )
                db.log_event(
                    conn, "user_status_changed", reel["id"],
                    **{"from": reel["user_status"], "to": status},
                )  # fmt: skip
            r.changed.append((reel["id"], reel["user_status"], status))
    return r
