import json

import pytest

from prism import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    with c:
        c.execute(
            "INSERT INTO reels (id, source, source_id, url, status)"
            " VALUES ('ig_x', 'mymind', 'card1', 'https://www.instagram.com/reel/x/', 'queued')"
        )
    return c


def events(conn):
    return [
        (r["type"], json.loads(r["payload"]))
        for r in conn.execute("SELECT type, payload FROM events ORDER BY id")
    ]


def status(conn):
    return conn.execute("SELECT * FROM reels WHERE id = 'ig_x'").fetchone()


def test_migrate_idempotent_and_seed(conn):
    assert db.migrate(conn) == 2
    slugs = [
        r[0] for r in conn.execute("SELECT slug FROM categories ORDER BY sort_order")
    ]
    assert slugs == ["konten-yt-ai", "config-claude-code", "ide-project"]


def test_valid_transition_writes_event(conn):
    db.transition(conn, "ig_x", "downloading")
    assert status(conn)["status"] == "downloading"
    assert events(conn) == [("stage_started", {"stage": "downloading"})]


def test_invalid_transition_rejected_without_side_effect(conn):
    with pytest.raises(ValueError, match="tidak valid"):
        db.transition(conn, "ig_x", "ready")
    assert status(conn)["status"] == "queued"
    assert events(conn) == []


def test_failed_requires_error(conn):
    db.transition(conn, "ig_x", "downloading")
    with pytest.raises(ValueError, match="error="):
        db.transition(conn, "ig_x", "failed")


def test_fail_then_retry_counts_attempts(conn):
    db.transition(conn, "ig_x", "downloading")
    db.transition(conn, "ig_x", "extracting")
    db.transition(conn, "ig_x", "failed", error="ffmpeg exit 1")
    assert status(conn)["stage_error"] == "extracting: ffmpeg exit 1"
    db.transition(conn, "ig_x", "queued")
    r = status(conn)
    assert (r["status"], r["attempts"], r["stage_error"]) == ("queued", 1, None)
    assert events(conn)[-2:] == [
        ("stage_failed", {"stage": "extracting", "error": "ffmpeg exit 1"}),
        ("reel_retried", {"from_stage": "extracting", "attempts": 1}),
    ]


def test_rate_limit_back_to_queued_not_counted(conn):
    db.transition(conn, "ig_x", "downloading")
    db.transition(conn, "ig_x", "queued")
    assert status(conn)["attempts"] == 0
    assert events(conn)[-1] == (
        "stage_failed",
        {"stage": "downloading", "error": "rate_limited"},
    )


@pytest.mark.parametrize(
    "error, to", [("curating: bad json", "ready"), ("downloading: x", "queued")]
)
def test_retry_target_from_failed_stage(conn, error, to):
    with conn:
        conn.execute(
            "UPDATE reels SET status = 'failed', stage_error = ? WHERE id = 'ig_x'",
            (error,),
        )
    assert db.retry(conn, "ig_x") == to
    r = status(conn)
    assert (r["status"], r["attempts"], r["stage_error"]) == (to, 1, None)
    assert events(conn)[-1] == (
        "reel_retried",
        {"from_stage": error.split(":")[0], "attempts": 1},
    )
