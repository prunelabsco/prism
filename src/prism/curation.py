"""Curation desk: `next` hands out a packet, `commit` stores the result. Rules in docs/CURATION.md."""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from prism import config, db, packet
from prism.models import Curation

STALE = "-1 hour"  # `curating` for longer than this counts as abandoned


class CurationError(ValueError):
    """Curation result rejected. The message lists every error; the DB is unchanged."""


@dataclass
class CommitResult:
    status: str  # curated | duplicate
    categories: list[str] = field(default_factory=list)
    idea_count: int = 0
    duplicate_of: str | None = None
    proposed: str | None = None


def next_packet(conn: sqlite3.Connection, reels_dir: Path | None = None) -> Path | None:
    """Reset abandoned `curating` reels, then move the oldest `ready` reel to curating."""
    reels_dir = reels_dir or config.settings.reels_dir
    for row in conn.execute(
        "SELECT id FROM reels WHERE status = 'curating'"
        " AND updated_at < datetime('now', ?)",
        (STALE,),
    ).fetchall():
        db.transition(conn, row["id"], "ready")

    row = conn.execute(
        "SELECT id FROM reels WHERE status = 'ready' ORDER BY updated_at, id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    db.transition(conn, row["id"], "curating")
    path = reels_dir / row["id"] / "packet.json"
    # the packet was built during process; other reels may have been curated since
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["recent_titles"] = packet.recent_titles(conn, row["id"])
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return path


def _require_curating(conn: sqlite3.Connection, reel_id: str) -> None:
    row = conn.execute("SELECT status FROM reels WHERE id = ?", (reel_id,)).fetchone()
    if row is None:
        raise CurationError(f"reel {reel_id} does not exist")
    if row["status"] != "curating":
        raise CurationError(
            f"reel {reel_id} is {row['status']}, only `curating` reels can be"
            " committed (take one with `prism next` first)"
        )


def _validate(conn: sqlite3.Connection, reel_id: str, text: str) -> Curation:
    cats = dict(conn.execute("SELECT slug, status FROM categories").fetchall())
    try:
        c = Curation.model_validate_json(text, context={"categories": cats})
    except ValidationError as e:
        lines = [
            f"- {'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}"
            for err in e.errors()
        ]
        raise CurationError("invalid curation.json:\n" + "\n".join(lines)) from None

    errors = []
    if c.reel_id != reel_id:
        errors.append(f"reel_id {c.reel_id} does not match argument {reel_id}")
    if c.duplicate_of is not None and (
        c.duplicate_of == reel_id
        or conn.execute(
            "SELECT 1 FROM reels WHERE id = ?", (c.duplicate_of,)
        ).fetchone()
        is None
    ):
        errors.append(f"duplicate_of {c.duplicate_of} is not another existing reel")
    if errors:
        raise CurationError("invalid curation.json:\n- " + "\n- ".join(errors))
    return c


def commit(
    conn: sqlite3.Connection,
    reel_id: str,
    path: Path,
    reels_dir: Path | None = None,
) -> CommitResult:
    """Validate + write ideas/evidence/proposed category + transition, one transaction."""
    reels_dir = reels_dir or config.settings.reels_dir
    _require_curating(conn, reel_id)
    c = _validate(conn, reel_id, path.read_text(encoding="utf-8"))

    r = CommitResult(status="duplicate" if c.duplicate_of else "curated")
    # transition() goes last: if it fails, the inserts above roll back with it
    with conn:
        if p := c.proposed_category:
            conn.execute(
                "INSERT INTO categories (slug, name, description, status,"
                " proposed_by, reason) VALUES (?, ?, ?, 'proposed', ?, ?)",
                (p.slug, p.name, p.description, reel_id, p.reason),
            )
            db.log_event(
                conn, "category_proposed", reel_id,
                slug=p.slug, name=p.name, reason=p.reason,
            )  # fmt: skip
            r.proposed = p.slug
        conn.executemany(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (reel_id, i.category, i.title, i.detail, i.why_relevant, i.confidence)
                for i in c.ideas
            ],
        )
        conn.executemany(
            "INSERT INTO evidence (reel_id, label, text, at_s) VALUES (?, ?, ?, ?)",
            [(reel_id, e.label, e.text, e.at_s) for e in c.evidence],
        )
        conn.execute(
            "UPDATE reels SET summary = ?, card_line = ?, duplicate_of = ? WHERE id = ?",
            (c.summary, c.card_line, c.duplicate_of, reel_id),
        )
        if c.duplicate_of:
            r.duplicate_of = c.duplicate_of
            db.transition(
                conn, reel_id, "duplicate",
                duplicate_of=c.duplicate_of, reason=c.duplicate_reason,
            )  # fmt: skip
        else:
            r.categories = list(dict.fromkeys(i.category for i in c.ideas))
            r.idea_count = len(c.ideas)
            db.transition(
                conn, reel_id, "curated",
                card_line=c.card_line, categories=r.categories,
                idea_count=r.idea_count,
            )  # fmt: skip

    # next_actions has no DB column, it lives in this file (read by export)
    out = reels_dir / reel_id / "curation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(c.model_dump_json(indent=2), encoding="utf-8")
    return r


def fail(conn: sqlite3.Connection, reel_id: str, reason: str) -> None:
    """Give up on curation (validation keeps failing). Auto-retry returns it to ready."""
    _require_curating(conn, reel_id)
    db.transition(conn, reel_id, "failed", error=reason[:300])


ACTIVE = ("core", "approved")  # valid targets for recategorize


def _proposed(conn: sqlite3.Connection, slug: str) -> None:
    row = conn.execute(
        "SELECT status FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise CurationError(f"category {slug} does not exist")
    if row["status"] != "proposed":
        raise CurationError(
            f"category {slug} is {row['status']}, only proposals can be decided"
        )


def approve(conn: sqlite3.Connection, slug: str) -> None:
    _proposed(conn, slug)
    with conn:
        conn.execute(
            "UPDATE categories SET status = 'approved' WHERE slug = ?", (slug,)
        )
        db.log_event(conn, "category_approved", slug=slug)


def reject(conn: sqlite3.Connection, slug: str) -> int:
    """Reject a proposal; its ideas become uncategorized. Returns the idea count."""
    _proposed(conn, slug)
    with conn:
        conn.execute(
            "UPDATE categories SET status = 'rejected' WHERE slug = ?", (slug,)
        )
        n = conn.execute(
            "UPDATE ideas SET category_slug = NULL WHERE category_slug = ?", (slug,)
        ).rowcount
        db.log_event(conn, "category_rejected", slug=slug)
    return n


def uncategorized(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, reel_id, title FROM ideas WHERE category_slug IS NULL ORDER BY id"
    ).fetchall()


def recategorize(conn: sqlite3.Connection, idea_id: int, slug: str) -> None:
    """Move an uncategorized idea into an active (not proposed) category."""
    idea = conn.execute(
        "SELECT reel_id, category_slug FROM ideas WHERE id = ?", (idea_id,)
    ).fetchone()
    if idea is None:
        raise CurationError(f"idea {idea_id} does not exist")
    if idea["category_slug"] is not None:
        raise CurationError(f"idea {idea_id} is already in {idea['category_slug']}")
    cat = conn.execute(
        "SELECT status FROM categories WHERE slug = ?", (slug,)
    ).fetchone()
    if cat is None or cat["status"] not in ACTIVE:
        got = "does not exist" if cat is None else f"is {cat['status']}"
        raise CurationError(f"category {slug} {got}, pick a core/approved category")
    with conn:
        conn.execute("UPDATE ideas SET category_slug = ? WHERE id = ?", (slug, idea_id))
        db.log_event(
            conn, "idea_recategorized", idea["reel_id"], idea_id=idea_id, slug=slug
        )


def categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every category + idea count: proposals first, then active, then rejected."""

    return conn.execute(
        "SELECT c.slug, c.name, c.status, c.reason, COUNT(i.id) AS ideas"
        " FROM categories c LEFT JOIN ideas i ON i.category_slug = c.slug"
        " GROUP BY c.slug ORDER BY"
        " CASE c.status WHEN 'proposed' THEN 0 WHEN 'rejected' THEN 2 ELSE 1 END,"
        " c.sort_order, c.name"
    ).fetchall()
