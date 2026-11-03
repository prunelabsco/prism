"""`curated` reels -> Markdown notes in output_dir. Template & no-overwrite rules in docs/OUTPUT.md."""

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from prism import config, db

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class ExportReport:
    written: int = 0  # new notes
    rewritten: int = 0  # existing notes whose content changed (e.g. recategorized)
    unchanged: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (reel_id, reason)


class _Dumper(yaml.SafeDumper):
    pass


# multi-line strings become `|` block scalars, not escaped strings
_Dumper.add_representer(
    str,
    lambda d, s: d.represent_scalar(
        "tag:yaml.org,2002:str", s, style="|" if "\n" in s else None
    ),
)


def read_frontmatter(text: str) -> dict:
    """YAML frontmatter at the top of a file. Empty if missing or broken (the user edits these)."""
    m = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mmss(sec: float | None) -> str:
    if sec is None:
        return ""
    return f" ({int(sec) // 60}:{int(sec) % 60:02d})"


def render(
    reel: sqlite3.Row,
    ideas: list[sqlite3.Row],
    evidence: list[sqlite3.Row],
    next_actions: list[str],
    day: str,
) -> str:
    """Full note text. Deterministic: same input, same text."""
    categories = list(
        dict.fromkeys(i["category_name"] for i in ideas if i["category_name"])
    )
    top = max((i["confidence"] for i in ideas), key=CONFIDENCE_RANK.get, default=None)
    front = {
        "prism_id": reel["id"],
        "source": reel["url"],
        "via": reel["source"],
        "source_id": reel["source_id"],
        "user_note": reel["user_note"] or "",
        "collections": json.loads(reel["source_tags"] or "[]"),
        "categories": categories,
        "status": "new",
        "confidence": top,
        "processed": date.fromisoformat(day),
        "tags": ["prism"],
    }
    fm = yaml.dump(
        front, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
        default_flow_style=None, width=1000,
    )  # fmt: skip

    out = [
        f"---\n{fm}---\n",
        "## Summary",
        reel["summary"] or "",
        "",
        "## Ideas",
    ]
    if not ideas:
        out.append("_No usable ideas._")
    for n, i in enumerate(ideas, 1):
        out += [
            f"### {n}. {i['title']}",
            i["detail"],
            f"**Why it matters:** {i['why_relevant']}",
            f"**Category:** {i['category_name'] or 'needs category'}",
            "",
        ]
    out += ["## Evidence"]
    labels = {"seen": "Seen", "guessed": "Guessed", "unknown": "Unknown"}
    out += [
        f"- **{labels[e['label']]}**{_mmss(e['at_s'])}: {e['text']}" for e in evidence
    ] or ["_None._"]
    out += ["", "## Next actions"]
    out += [f"- [ ] {a}" for a in next_actions] or ["_None._"]
    out += [
        "",
        "## My notes",
        "<!-- prism: free area, never touched by prism -->",
    ]
    return "\n".join(out) + "\n"


def _filename(day: str, title: str) -> str:
    t = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", title)
    t = re.sub(r"\s+", " ", t).strip(" .")[:60].strip(" .")
    return f"{day} {t or 'note'}.md"


def _write_atomic(path: Path, text: str) -> bytes:
    data = text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def _gather(conn: sqlite3.Connection, reel: sqlite3.Row, reels_dir: Path):
    ideas = conn.execute(
        "SELECT i.title, i.detail, i.why_relevant, i.confidence, c.name AS category_name"
        " FROM ideas i LEFT JOIN categories c ON c.slug = i.category_slug"
        " WHERE i.reel_id = ? ORDER BY i.id",
        (reel["id"],),
    ).fetchall()
    evidence = conn.execute(
        "SELECT label, text, at_s FROM evidence WHERE reel_id = ? ORDER BY id",
        (reel["id"],),
    ).fetchall()
    cur = reels_dir / reel["id"] / "curation.json"
    actions = (
        json.loads(cur.read_text("utf-8")).get("next_actions", [])
        if cur.exists()
        else []
    )
    at = conn.execute(
        "SELECT at FROM events WHERE reel_id = ? AND type = 'reel_curated'"
        " ORDER BY id DESC LIMIT 1",
        (reel["id"],),
    ).fetchone()
    day = at["at"][:10] if at else date.today().isoformat()
    return ideas, evidence, actions, day


def _blocked(path: Path, reel: sqlite3.Row, out_dir: Path) -> str | None:
    """Why an existing note must not be overwritten, None if it is safe."""
    if not path.is_file():
        return "no file at note_path (moved/deleted?), run `prism sync`"
    if path.parent != out_dir:
        return "moved out of output_dir"
    data = path.read_bytes()
    if reel["export_hash"] is None or sha256(data) != reel["export_hash"]:
        return "edited by you"
    status = read_frontmatter(data.decode("utf-8", errors="replace")).get("status")
    if status != "new":
        return f"status {status}"
    return None


def run(
    conn: sqlite3.Connection,
    out_dir: Path | None = None,
    reels_dir: Path | None = None,
) -> ExportReport:
    """Idempotent: re-running never overwrites your edits nor duplicates files."""
    out_dir = out_dir or config.settings.output_dir
    reels_dir = reels_dir or config.settings.reels_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    r = ExportReport()

    reels = conn.execute(
        "SELECT * FROM reels WHERE status IN ('curated', 'exported') ORDER BY created_at, id"
    ).fetchall()
    for reel in reels:
        ideas, evidence, actions, day = _gather(conn, reel, reels_dir)
        text = render(reel, ideas, evidence, actions, day)

        if reel["status"] == "exported":
            path = Path(reel["note_path"] or "")
            why = _blocked(path, reel, out_dir)
            if why:
                r.skipped.append((reel["id"], why))
            elif sha256(text.encode()) == reel["export_hash"]:
                r.unchanged += 1
            else:
                h = sha256(_write_atomic(path, text))
                with conn:
                    conn.execute(
                        "UPDATE reels SET export_hash = ? WHERE id = ?", (h, reel["id"])
                    )
                r.rewritten += 1
            continue

        title = ideas[0]["title"] if ideas else (reel["card_line"] or reel["id"])
        path = out_dir / _filename(day, title)
        if path.exists():
            path = out_dir / _filename(day, f"{title} {reel['id']}")
        if path.exists():
            r.skipped.append((reel["id"], f"{path.name} already exists"))

            continue
        h = sha256(_write_atomic(path, text))
        with conn:
            conn.execute(
                "UPDATE reels SET note_path = ?, export_hash = ? WHERE id = ?",
                (str(path), h, reel["id"]),
            )
            db.transition(conn, reel["id"], "exported", note_path=str(path))
        r.written += 1
    return r
