"""Build packet.json: the only thing the agent reads while curating."""

import json
import sqlite3
from pathlib import Path

from prism import config

RECENT_LIMIT = 30


def _read(out_dir: Path, name: str):
    return json.loads((out_dir / name).read_text(encoding="utf-8"))


def recent_titles(conn: sqlite3.Connection, reel_id: str) -> list[dict]:
    """Already-curated reels, for duplicate checks. reel_id is included so the agent can fill duplicate_of."""
    rows = conn.execute(
        "SELECT id AS reel_id, COALESCE(card_line, title) AS title FROM reels"
        " WHERE status IN ('curated', 'exported') AND id != ?"
        " ORDER BY updated_at DESC, id LIMIT ?",
        (reel_id, RECENT_LIMIT),
    ).fetchall()
    return [dict(r) for r in rows]


def categories(conn: sqlite3.Connection) -> list[dict]:
    # proposed included so the agent reuses a pending proposal instead of a twin
    rows = conn.execute(
        "SELECT slug, name, status, description FROM categories"
        " WHERE status IN ('core', 'approved', 'proposed')"
        " ORDER BY status = 'proposed', sort_order, name"
    ).fetchall()
    return [dict(c) for c in rows]


def collection_categories(
    conn: sqlite3.Connection, collections: list[str]
) -> list[str]:
    """Active categories the user mapped this reel's collections to."""
    active = {
        row["slug"]
        for row in conn.execute(
            "SELECT slug FROM categories WHERE status IN ('core', 'approved')"
        )
    }
    mapped = (config.settings.collections.get(c) for c in collections)
    return list(dict.fromkeys(slug for slug in mapped if slug in active))


def build(conn: sqlite3.Connection, reel_id: str, out_dir: Path) -> dict:
    """Merge reel row + meta/frames/transcript + categories, write packet.json."""
    reel = conn.execute(
        "SELECT id, source, url, title, user_note, source_tags FROM reels WHERE id = ?",
        (reel_id,),
    ).fetchone()
    if reel is None:
        raise ValueError(f"reel {reel_id} does not exist")
    meta = _read(out_dir, "meta.json")
    transcript = _read(out_dir, "transcript.json")
    collections = json.loads(reel["source_tags"] or "[]")

    packet = {
        "reel_id": reel["id"],
        "source": reel["source"],
        "url": reel["url"],
        "title": reel["title"],
        "user_note": reel["user_note"],
        # Instagram collection names = the user's own topic hint
        "collections": collections,
        # the user's own sorting, see `[collections]` in config.toml
        "collection_categories": collection_categories(conn, collections),
        "media": meta.get("media", "video"),  # photo: frame = slide, at_s null
        "caption": meta.get("caption"),
        "duration_s": meta["duration_s"],
        "has_speech": transcript["has_speech"],
        "transcript": transcript["segments"],
        "frames": _read(out_dir, "frames.json"),
        "categories": categories(conn),
        "recent_titles": recent_titles(conn, reel_id),
    }
    (out_dir / "packet.json").write_text(
        json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return packet
