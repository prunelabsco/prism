"""Queue -> download, frames, transcript, packet -> ready."""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prism import db, download, frames, packet, transcribe
from prism.config import settings

log = logging.getLogger("prism.process")

# a local stage failing fails that reel, the run continues. OSError/ValueError/KeyError: broken files
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
    retried: int = 0  # failed -> queued automatically
    downloaded: int = 0
    cached: int = 0  # video.mp4 + meta.json already there
    transient: int = 0  # rate limit/login/network, back to queued
    extracted: int = 0  # frames extracted this run
    transcribed: int = 0
    ready: int = 0  # packet built, ready to curate
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
    progress: Callable[[str], None] = lambda _: None,
) -> ProcessReport:
    """Safe to re-run: reels stuck mid-stage are picked up again.

    `limit`/`max_per_run` only cap downloads (the part that touches Instagram).
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

    # local stages in order: (status, done?, run, report field)
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
    to_download = min(
        cap, sum(row["status"] in ("queued", "downloading") for row in rows)
    )
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
                progress(f"{reel_id}: downloading ({attempts}/{to_download})")
                if status == "queued":
                    db.transition(conn, reel_id, "downloading")
                    status = "downloading"
                try:
                    fetch_video(row["url"], out)
                except download.IgError as e:
                    log.warning("download %s failed: %s", reel_id, e)
                    if e.transient:
                        db.transition(
                            conn, reel_id, "queued", error=download.error_kind(e)
                        )
                        r.transient += 1
                        progress(f"{reel_id}: temporary error, back in the queue")
                        fails += 1
                        if fails >= 2:
                            r.stopped = download.stop_reason(e)
                    else:
                        db.transition(conn, reel_id, "failed", error=str(e)[:300])
                        r.failed += 1
                        progress(f"{reel_id}: download failed")
                        # "empty media" = dead post OR not logged in to Instagram:
                        # still failed, but twice in a row stops the run
                        empty = empty + 1 if EMPTY_MEDIA in str(e).lower() else 0
                        fails = 0
                        if empty >= 2:
                            r.stopped = f"empty media twice in a row, check your Instagram login: {e}"
                    continue
                fails = empty = 0
                r.downloaded += 1
            if status == "queued":
                db.transition(conn, reel_id, "downloading")
                status = "downloading"

        for stage, is_complete, fn, field in local:
            if status in order and order.index(status) > order.index(stage):
                continue  # stage already passed
            if status != stage:
                db.transition(conn, reel_id, stage)
                status = stage
            if is_complete(out):
                continue
            progress(f"{reel_id}: {stage}")
            try:
                fn(out)
            except STAGE_ERRORS as e:
                log.warning("%s %s failed: %s", stage, reel_id, e)
                db.transition(conn, reel_id, "failed", error=str(e)[:300])
                r.failed += 1
                progress(f"{reel_id}: {stage} failed")
                break
            setattr(r, field, getattr(r, field) + 1)
        else:
            try:
                build_packet(conn, reel_id, out)
            except STAGE_ERRORS as e:
                log.warning("packet %s failed: %s", reel_id, e)
                db.transition(conn, reel_id, "failed", error=str(e)[:300])
                r.failed += 1
                progress(f"{reel_id}: packet failed")
                continue
            db.transition(conn, reel_id, "ready")
            r.ready += 1
            progress(f"{reel_id}: ready")
            # only after ready: a crash before this never loses an unfinished reel's video

            if not keep_video:
                (out / "video.mp4").unlink(missing_ok=True)
    return r
