"""Antrian -> download, frame, transkrip, paket -> ready. Aturan di PIPELINE.md."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prism import db, download, frames, packet, transcribe
from prism.config import settings

log = logging.getLogger("prism.process")

# tahap lokal gagal = reel gagal, run lanjut. OSError/ValueError/KeyError: file rusak
STAGE_ERRORS = (
    frames.FrameError,
    transcribe.TranscribeError,
    OSError,
    ValueError,
    KeyError,
)


EMPTY_MEDIA = "empty media"


@dataclass
class ProcessReport:
    retried: int = 0  # failed -> queued otomatis
    downloaded: int = 0
    cached: int = 0  # video.mp4 + meta.json sudah ada
    rate_limited: int = 0  # balik ke queued
    extracted: int = 0  # frame diekstrak run ini
    transcribed: int = 0
    ready: int = 0  # paket jadi, siap dikurasi
    failed: int = 0
    stopped: str | None = None


def run(
    conn: sqlite3.Connection,
    limit: int | None = None,
    fetch_video: Callable[[str, Path], None] = download.fetch_video,
    throttle: Callable[[], None] = download.throttle,
    extract: Callable[[Path], object] = frames.extract,
    transcribe_fn: Callable[[Path], object] = transcribe.transcribe,
    build_packet: Callable[[sqlite3.Connection, str, Path], object] = packet.build,
    keep_video: bool | None = None,
    reels_dir: Path | None = None,
) -> ProcessReport:
    """Aman dijalankan berulang: reel nyangkut di tengah tahap diambil lagi.

    `limit`/`max_per_run` hanya membatasi download (yang menyentuh Instagram).
    """
    reels_dir = reels_dir or settings.reels_dir
    if keep_video is None:
        keep_video = settings.download.keep_video
    cap = settings.download.max_per_run
    cap = min(limit, cap) if limit is not None else cap
    r = ProcessReport()

    for row in conn.execute(
        "SELECT id FROM reels WHERE status = 'failed' AND attempts < ?",
        (db.MAX_ATTEMPTS,),
    ).fetchall():
        db.retry(conn, row["id"])
        r.retried += 1

    # tahap lokal berurutan: (status, sudah selesai?, jalankan, field laporan)
    local = [
        ("extracting", frames.is_complete, extract, "extracted"),
        ("transcribing", transcribe.is_complete, transcribe_fn, "transcribed"),
    ]
    order = [st for st, *_ in local]

    rows = conn.execute(
        "SELECT id, url, status FROM reels"
        " WHERE status IN ('transcribing', 'extracting', 'downloading', 'queued')"
        " ORDER BY status = 'queued', created_at, id"
    ).fetchall()
    attempts = fails = empty = 0
    for row in rows:
        reel_id, status, out = row["id"], row["status"], reels_dir / row["id"]

        if status in ("queued", "downloading"):
            if download.is_complete(out):
                r.cached += 1
            else:
                if r.stopped or attempts >= cap:
                    continue
                if attempts:
                    throttle()
                attempts += 1
                if status == "queued":
                    db.transition(conn, reel_id, "downloading")
                    status = "downloading"
                try:
                    fetch_video(row["url"], out)
                except download.IgError as e:
                    log.warning("download %s gagal: %s", reel_id, e)
                    if e.transient:
                        db.transition(conn, reel_id, "queued")
                        r.rate_limited += 1
                        fails += 1
                        if fails >= 2:
                            r.stopped = str(e)
                    else:
                        db.transition(conn, reel_id, "failed", error=str(e)[:300])
                        r.failed += 1
                        # "empty media" = post mati ATAU sesi IG tidak login:
                        # tetap failed, tapi 2x berturut-turut hentikan run
                        empty = empty + 1 if EMPTY_MEDIA in str(e).lower() else 0
                        fails = 0
                        if empty >= 2:
                            r.stopped = (
                                f"empty media 2x berturut-turut, cek login IG: {e}"
                            )
                    continue
                fails = empty = 0
                r.downloaded += 1
            if status == "queued":
                db.transition(conn, reel_id, "downloading")
                status = "downloading"

        for stage, is_complete, fn, field in local:
            if status in order and order.index(status) > order.index(stage):
                continue  # tahap ini sudah lewat
            if status != stage:
                db.transition(conn, reel_id, stage)
                status = stage
            if is_complete(out):
                continue
            try:
                fn(out)
            except STAGE_ERRORS as e:
                log.warning("%s %s gagal: %s", stage, reel_id, e)
                db.transition(conn, reel_id, "failed", error=str(e)[:300])
                r.failed += 1
                break
            setattr(r, field, getattr(r, field) + 1)
        else:
            try:
                build_packet(conn, reel_id, out)
            except STAGE_ERRORS as e:
                log.warning("packet %s gagal: %s", reel_id, e)
                db.transition(conn, reel_id, "failed", error=str(e)[:300])
                r.failed += 1
                continue
            db.transition(conn, reel_id, "ready")
            r.ready += 1
            # setelah ready: crash di sini tidak membuang video reel yang belum jadi
            if not keep_video:
                (out / "video.mp4").unlink(missing_ok=True)
    return r
