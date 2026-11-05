"""Source -> queue: normalize URLs, dedup, probe posts, insert reels."""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from prism import db, download
from prism.config import settings
from prism.sources.base import SourceAdapter, SourceItem

log = logging.getLogger("prism.fetch")

# [username/](reel|reels|p|tv)/<shortcode>, query & tracking ignored
_IG = re.compile(
    r"instagram\.com/(?:[\w.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE
)


@dataclass
class FetchReport:
    new_reel: int = 0
    new_post: int = 0
    new_photo: int = 0  # post without video, curated from its photo slides
    unreachable: int = 0  # post unreachable MAX_ATTEMPTS times -> skipped
    deferred: int = 0  # post not probed yet (run cap / hiccup)
    existing: int = 0
    duplicates: int = 0  # same reel already queued from another source
    not_instagram: int = 0
    probe_stopped: str | None = None


def shortcode(url: str) -> tuple[str, str] | None:
    """-> (path kind, shortcode), or None if not an Instagram reel/post."""
    m = _IG.search(url)
    return (m.group(1).lower(), m.group(2)) if m else None


def canonical_url(code: str) -> str:
    # ponytail: one form for reels & posts, yt-dlp accepts /p/ for reels too
    return f"https://www.instagram.com/p/{code}/"


def _log_dup_once(conn: sqlite3.Connection, reel_id: str, source_id: str) -> None:
    seen = conn.execute(
        "SELECT 1 FROM events WHERE type = 'source_duplicate' AND reel_id = ?"
        " AND json_extract(payload, '$.source_id') = ?",
        (reel_id, source_id),
    ).fetchone()
    if not seen:
        with conn:
            db.log_event(conn, "source_duplicate", reel_id, source_id=source_id)


def _count_probe_failed(conn: sqlite3.Connection, reel_id: str, error: str) -> int:
    """Record one permanent-ish failure, return this reel's failure total."""
    with conn:
        db.log_event(conn, "probe_failed", reel_id, error=error[:300])
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'probe_failed' AND reel_id = ?",
        (reel_id,),
    ).fetchone()[0]


def _insert(
    conn: sqlite3.Connection,
    reel_id: str,
    code: str,
    item: SourceItem,
    status: str,
    reason: str = "not_video",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, title, user_note,"
            " saved_at, source_tags, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reel_id,
                item.source,
                item.source_id,
                canonical_url(code),
                item.title,
                item.user_note,
                item.saved_at,
                json.dumps(item.tags, ensure_ascii=False),
                status,
            ),
        )
        extra = {"reason": reason} if status == "skipped" else {}
        db.log_event(
            conn,
            "reel_queued" if status == "queued" else "reel_skipped",
            reel_id,
            title=item.title,
            source=item.source,
            **extra,
        )


def run(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    limit: int | None = None,
    max_probes: int | None = None,
    probe: Callable[[str], bool] = download.probe,
    throttle: Callable[[], None] = download.throttle,
    progress: Callable[[str], None] = lambda _: None,
) -> FetchReport:
    """Idempotent: re-running adds no duplicate reels or events."""
    if max_probes is None:
        max_probes = settings.download.max_per_run
    r = FetchReport()
    probes = fails = 0

    items = list(adapter.list_new())
    progress(
        f"{len(items)} saved items. Reels queue instantly; up to {max_probes} posts"
        " are checked for video, a few seconds each. This can take minutes."
    )
    for item in items:
        sc = shortcode(item.url)
        if sc is None:
            r.not_instagram += 1
            continue
        kind, code = sc
        reel_id = f"ig_{code}"

        row = conn.execute(
            "SELECT source_id FROM reels WHERE id = ?", (reel_id,)
        ).fetchone()
        if row:
            if row["source_id"] == item.source_id:
                r.existing += 1
            else:
                r.duplicates += 1
                _log_dup_once(conn, reel_id, item.source_id)
            continue

        if limit is not None and r.new_reel + r.new_post + r.new_photo >= limit:
            continue  # keep looping so the existing/duplicate counts are complete

        if kind != "p":  # /reel/ is always a video; /p/ may be photos
            _insert(conn, reel_id, code, item, "queued")
            r.new_reel += 1
            continue

        if r.probe_stopped or probes >= max_probes:
            r.deferred += 1
            continue
        if probes:
            throttle()
        probes += 1
        progress(f"checking post {probes}/{max_probes}: {reel_id}")
        try:
            has_video = probe(canonical_url(code))
        except download.IgError as e:
            log.warning("probe %s failed: %s", reel_id, e)
            if not e.transient and (
                _count_probe_failed(conn, reel_id, str(e)) >= db.MAX_ATTEMPTS
            ):
                _insert(conn, reel_id, code, item, "skipped", "unreachable")

                r.unreachable += 1
            else:
                r.deferred += 1
            fails += 1
            if fails >= 2:
                r.probe_stopped = download.stop_reason(e)
            continue
        fails = 0
        _insert(conn, reel_id, code, item, "queued")
        if has_video:
            r.new_post += 1
        else:
            r.new_photo += 1
    return r
