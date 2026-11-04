import pytest

from prism import db, download, frames, process, transcribe


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


def add(conn, *ids, status="queued", attempts=0, error=None):
    with conn:
        for i in ids:
            conn.execute(
                "INSERT INTO reels (id, source, source_id, url, status, attempts,"
                " stage_error) VALUES (?, 'ig_export', ?, ?, ?, ?, ?)",
                (i, i, f"https://www.instagram.com/p/{i}/", status, attempts, error),
            )


class FakeFetch:
    def __init__(self, results=None):
        self.results = results or {}  # id -> IgError
        self.calls = []

    def __call__(self, url, out):
        rid = out.name
        self.calls.append(rid)
        if rid in self.results:
            raise self.results[rid]
        out.mkdir(parents=True, exist_ok=True)
        (out / "video.mp4").write_bytes(b"v")
        (out / "meta.json").write_text("{}")


def fake_extract(out):
    (out / "frames.json").write_text("[]")


def fake_transcribe(out):
    (out / "transcript.json").write_text("{}")


def fake_packet(conn, reel_id, out):
    (out / "packet.json").write_text("{}")


def run(
    conn,
    tmp_path,
    fetch,
    extract=fake_extract,
    transcribe_fn=fake_transcribe,
    build_packet=fake_packet,
    **kw,
):
    return process.run(
        conn,
        fetch_video=fetch,
        throttle=lambda: None,
        extract=extract,
        transcribe_fn=transcribe_fn,
        build_packet=build_packet,
        reels_dir=tmp_path,
        **kw,
    )


def statuses(conn):
    return dict(conn.execute("SELECT id, status FROM reels").fetchall())


def test_download_ok_and_limit(conn, tmp_path):
    add(conn, "a", "b", "c")
    f = FakeFetch()
    r = run(conn, tmp_path, f, limit=2)
    assert r.downloaded == 2 and r.extracted == 2 and f.calls == ["a", "b"]
    assert statuses(conn) == {"a": "ready", "b": "ready", "c": "queued"}
    # rerun: a, b not downloaded/extracted again
    f2 = FakeFetch()
    r = run(conn, tmp_path, f2)
    assert f2.calls == ["c"] and r.extracted == 1


def test_timeout_is_reported_as_network_not_rate_limit(conn, tmp_path):
    add(conn, "a", "b")
    err = download.IgError("Read timed out. (read timeout=20.0)", transient=True)
    r = run(conn, tmp_path, FakeFetch({"a": err, "b": err}))
    assert "network" in r.stopped and "rate limit" not in r.stopped


def test_stops_after_two_rate_limits(conn, tmp_path):
    add(conn, "a", "b", "c")
    rl = download.IgError("rate-limit reached", transient=True)
    r = run(conn, tmp_path, FakeFetch({"a": rl, "b": rl}))
    assert r.transient == 2 and "rate limit" in r.stopped
    assert statuses(conn) == {"a": "queued", "b": "queued", "c": "queued"}
    a = conn.execute("SELECT attempts FROM reels WHERE id = 'a'").fetchone()[0]
    assert a == 0


def test_permanent_error_fails_without_stopping(conn, tmp_path):
    add(conn, "a", "b", "c")
    dead = download.IgError("not available", transient=False)
    r = run(conn, tmp_path, FakeFetch({"a": dead, "b": dead}))
    assert r.failed == 2 and r.downloaded == 1 and not r.stopped
    assert statuses(conn)["a"] == "failed"


def test_stops_after_two_empty_media(conn, tmp_path):
    add(conn, "a", "b", "c")
    empty = download.IgError("Instagram sent an empty media response", transient=False)
    f = FakeFetch({"a": empty, "b": empty})
    r = run(conn, tmp_path, f)
    assert r.failed == 2 and r.stopped and f.calls == ["a", "b"]
    assert statuses(conn) == {"a": "failed", "b": "failed", "c": "queued"}


def test_auto_retry_and_stuck_downloading(conn, tmp_path):
    add(conn, "f1", status="failed", attempts=1, error="downloading: x")
    add(conn, "f3", status="failed", attempts=db.MAX_ATTEMPTS, error="downloading: x")
    add(conn, "stuck", status="downloading")
    f = FakeFetch()
    r = run(conn, tmp_path, f)
    assert r.retried == 1
    assert sorted(f.calls) == ["f1", "stuck"]
    assert statuses(conn) == {
        "f1": "ready",
        "f3": "failed",
        "stuck": "ready",
    }


def test_auto_retry_curating_failure_back_to_ready(conn, tmp_path):
    add(conn, "c1", status="failed", attempts=1, error="curating: bad json")
    f = FakeFetch()
    r = run(conn, tmp_path, f)
    assert r.retried == 1 and f.calls == []
    assert statuses(conn) == {"c1": "ready"}


def test_cached_download_goes_to_extract(conn, tmp_path):
    add(conn, "done", status="downloading")
    FakeFetch()("u", tmp_path / "done")  # files left by an earlier run
    f = FakeFetch()
    r = run(conn, tmp_path, f, limit=0)  # limit only caps downloads
    assert f.calls == [] and r.cached == 1 and r.extracted == 1
    assert statuses(conn) == {"done": "ready"}


def test_stuck_extracting_reextracted_or_skipped(conn, tmp_path):
    add(conn, "a", "b", status="extracting")
    (tmp_path / "a").mkdir()
    fake_extract(tmp_path / "a")
    seen = []
    r = run(conn, tmp_path, FakeFetch(), extract=lambda out: seen.append(out.name))
    assert seen == ["b"] and r.extracted == 1


def test_frame_error_fails(conn, tmp_path):
    add(conn, "a")

    def boom(out):
        raise frames.FrameError("ffmpeg failed")

    r = run(conn, tmp_path, FakeFetch(), extract=boom)
    assert r.failed == 1
    row = conn.execute("SELECT status, stage_error FROM reels").fetchone()
    assert tuple(row) == ("failed", "extracting: ffmpeg failed")


def test_transcribe_stage(conn, tmp_path):
    add(conn, "a", "b", "c", status="transcribing")
    (tmp_path / "a").mkdir()
    fake_transcribe(tmp_path / "a")  # already done, skipped

    def tr(out):
        if out.name == "b":
            raise transcribe.TranscribeError("whisper failed")
        out.mkdir(exist_ok=True)
        fake_transcribe(out)

    r = run(conn, tmp_path, FakeFetch(), transcribe_fn=tr)
    assert r.transcribed == 1 and r.failed == 1 and r.extracted == 0
    assert statuses(conn) == {"a": "ready", "b": "failed", "c": "ready"}
    err = conn.execute("SELECT stage_error FROM reels WHERE id = 'b'").fetchone()[0]
    assert err == "transcribing: whisper failed"


@pytest.mark.parametrize("keep", [False, True])
def test_ready_and_keep_video(conn, tmp_path, keep):
    add(conn, "a")
    r = run(conn, tmp_path, FakeFetch(), keep_video=keep)
    assert r.ready == 1 and statuses(conn) == {"a": "ready"}
    assert (tmp_path / "a" / "packet.json").exists()
    assert (tmp_path / "a" / "video.mp4").exists() is keep
    ev = conn.execute("SELECT type FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert ev[0] == "reel_ready"
    # rerun: ready reels untouched
    assert run(conn, tmp_path, FakeFetch()).ready == 0


def test_packet_error_fails_and_keeps_video(conn, tmp_path):
    add(conn, "a")

    def boom(conn, reel_id, out):
        raise KeyError("duration_s")

    r = run(conn, tmp_path, FakeFetch(), build_packet=boom, keep_video=False)
    assert r.failed == 1 and r.ready == 0
    assert statuses(conn) == {"a": "failed"}
    assert (tmp_path / "a" / "video.mp4").exists()
