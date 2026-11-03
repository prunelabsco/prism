"""Koneksi SQLite, migrasi, transition(), log_event(). Aturan di .docs/DATA.md."""

import json
import sqlite3
from pathlib import Path

from prism.config import settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# reel failed dengan attempts < ini diambil ulang otomatis oleh `prism process`
MAX_ATTEMPTS = 3

STATUS_ORDER = (
    "queued", "downloading", "extracting", "transcribing", "ready",
    "curating", "curated", "duplicate", "exported", "failed", "skipped",
)  # fmt: skip

# (dari, ke) -> tipe event. Pasangan di luar tabel ini = transisi tidak valid.
TRANSITIONS = {
    ("queued", "downloading"): "stage_started",
    ("downloading", "extracting"): "stage_started",
    ("extracting", "transcribing"): "stage_started",
    ("transcribing", "ready"): "reel_ready",
    ("ready", "curating"): "curation_started",
    ("curating", "curated"): "reel_curated",
    ("curating", "duplicate"): "duplicate_detected",
    ("curating", "ready"): "curation_reset",
    ("curated", "exported"): "reel_exported",
    ("downloading", "failed"): "stage_failed",
    ("extracting", "failed"): "stage_failed",
    ("transcribing", "failed"): "stage_failed",
    ("curating", "failed"): "stage_failed",
    ("downloading", "queued"): "stage_failed",  # rate limit, bukan gagal
    ("failed", "queued"): "reel_retried",
    ("failed", "ready"): "reel_retried",  # gagal di curating, paket masih ada
    ("skipped", "queued"): "reel_retried",  # post foto lama, sekarang ikut dikurasi
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Jalankan migrasi yang belum diterapkan. Return versi skema sekarang."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for file in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        num = int(file.name[:3])
        if num <= version:
            continue
        # executescript commit sendiri, jadi BEGIN/COMMIT ditulis eksplisit
        conn.executescript(
            f"BEGIN;\n{file.read_text()}\nPRAGMA user_version = {num};\nCOMMIT;"
        )
        version = num
    return version


class NotInitialized(Exception):
    pass


def open_existing(path: Path | None = None) -> sqlite3.Connection:
    """Untuk semua perintah selain `init`: tolak kalau DB belum ada, migrasi baru otomatis."""
    path = path or settings.db_path
    if not path.exists():
        raise NotInitialized(f"Belum di-init ({path}). Jalankan `prism init`.")
    conn = connect(path)
    migrate(conn)
    return conn


def status_summary(conn: sqlite3.Connection) -> dict:
    """Data mentah untuk `prism status`. Rendering di cli.py."""
    counts = dict(
        conn.execute("SELECT status, COUNT(*) FROM reels GROUP BY status").fetchall()
    )
    auto_retry = conn.execute(
        "SELECT COUNT(*) FROM reels WHERE status = 'failed' AND attempts < ?",
        (MAX_ATTEMPTS,),
    ).fetchone()[0]
    proposed = conn.execute(
        "SELECT slug, name, reason FROM categories WHERE status = 'proposed'"
        " ORDER BY created_at"
    ).fetchall()
    active = conn.execute(
        "SELECT c.name, COUNT(i.id) AS ideas FROM categories c"
        " LEFT JOIN ideas i ON i.category_slug = c.slug"
        " WHERE c.status IN ('core', 'approved')"
        " GROUP BY c.slug ORDER BY c.sort_order, c.name"
    ).fetchall()
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE category_slug IS NULL"
    ).fetchone()[0]
    return {
        "counts": counts,
        "uncategorized": uncategorized,
        "failed_auto_retry": auto_retry,
        "proposed": proposed,
        "active": active,
    }


def log_event(
    conn: sqlite3.Connection, type: str, reel_id: str | None = None, **payload
) -> None:
    """Append-only. Caller yang commit (atau bungkus dengan `with conn`)."""
    conn.execute(
        "INSERT INTO events (reel_id, type, payload) VALUES (?, ?, ?)",
        (reel_id, type, json.dumps(payload, ensure_ascii=False)),
    )


def transition(
    conn: sqlite3.Connection, reel_id: str, to_status: str, **payload
) -> None:
    """Ubah status reel + tulis satu event dalam satu transaksi.

    Ke `failed` wajib menyertakan `error=`. Update kolom lain dari caller
    (summary, duplicate_of, dst) yang belum di-commit ikut masuk transaksi ini.
    """
    row = conn.execute(
        "SELECT status, stage_error, attempts FROM reels WHERE id = ?", (reel_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"reel {reel_id} tidak ada")
    frm = row["status"]
    event = TRANSITIONS.get((frm, to_status))
    if event is None:
        raise ValueError(f"transisi tidak valid: {reel_id} {frm} -> {to_status}")

    sets = {"status": to_status}
    if event == "stage_started":
        payload = {"stage": to_status, **payload}
    elif to_status == "failed":
        if "error" not in payload:
            raise ValueError("transisi ke failed wajib menyertakan error=")
        payload = {"stage": frm, **payload}
        sets["stage_error"] = f"{frm}: {payload['error']}"
    elif (frm, to_status) == ("downloading", "queued"):
        payload = {"stage": frm, "error": "rate_limited", **payload}
    elif frm == "skipped":
        payload = {"from_stage": "skipped", **payload}  # belum pernah gagal
    elif event == "reel_retried":
        sets["attempts"] = row["attempts"] + 1
        sets["stage_error"] = None
        from_stage = (row["stage_error"] or "").split(":", 1)[0] or None
        payload = {"from_stage": from_stage, "attempts": sets["attempts"], **payload}

    with conn:
        cols = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(
            f"UPDATE reels SET {cols}, updated_at = datetime('now') WHERE id = ?",
            (*sets.values(), reel_id),
        )
        log_event(conn, event, reel_id, **payload)


def retry(conn: sqlite3.Connection, reel_id: str) -> str:
    """Reel `failed` diulang dari tahap yang gagal. Return status tujuan.

    Gagal di `curating` balik ke `ready` (paket masih ada, video sudah dihapus),
    selain itu ke `queued`.
    """
    row = conn.execute(
        "SELECT stage_error FROM reels WHERE id = ?", (reel_id,)
    ).fetchone()
    to = (
        "ready"
        if row and (row["stage_error"] or "").startswith("curating:")
        else "queued"
    )
    transition(conn, reel_id, to)
    return to


def picked_ideas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Ide dari reel berstatus `dipilih`, urut kategori. Dasar `prism status --picked`."""
    return conn.execute(
        "SELECT COALESCE(c.name, 'perlu kategori') AS category, i.reel_id, i.title,"
        " i.confidence, r.note_path FROM ideas i JOIN reels r ON r.id = i.reel_id"
        " LEFT JOIN categories c ON c.slug = i.category_slug"
        " WHERE r.user_status = 'dipilih'"
        " ORDER BY c.sort_order IS NULL, c.sort_order, c.name, i.reel_id, i.id"
    ).fetchall()
