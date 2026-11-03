import json

import pytest

from prism import db, download, fetch
from prism.sources.base import SourceItem


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://www.instagram.com/reel/Dei8-kWDrmV/?srtk=MWtl",
            ("reel", "Dei8-kWDrmV"),
        ),
        ("https://instagram.com/reels/AbC_1/", ("reels", "AbC_1")),
        ("https://www.instagram.com/p/DeSDymhieSd/?img_index=2", ("p", "DeSDymhieSd")),
        ("https://www.instagram.com/some.user/reel/XYZ/", ("reel", "XYZ")),
        ("https://www.instagram.com/tv/TV1/", ("tv", "TV1")),
        ("https://www.instagram.com/some.user/", None),
        ("https://www.instagram.com/stories/u/123/", None),
        ("https://example.com/p/abc/", None),
    ],
)
def test_shortcode(url, expected):
    assert fetch.shortcode(url) == expected


def item(sid, url, title="t"):
    return SourceItem(source="ig_export", source_id=sid, url=url, title=title)


class FakeAdapter:
    def __init__(self, items):
        self.items = items

    def list_new(self):
        return self.items


class FakeProbe:
    def __init__(self, results):
        self.results = dict(results)  # shortcode -> bool | Exception
        self.calls = []

    def __call__(self, url):
        code = url.rstrip("/").rsplit("/", 1)[1]
        self.calls.append(code)
        res = self.results[code]
        if isinstance(res, Exception):
            raise res
        return res


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


def run(conn, items, probe=None, **kw):
    return fetch.run(
        conn,
        FakeAdapter(items),
        probe=probe or FakeProbe({}),
        throttle=lambda: None,
        **kw,
    )


def rows(conn):
    return {
        r["id"]: (r["status"], r["url"])
        for r in conn.execute("SELECT id, status, url FROM reels")
    }


def events(conn, type):
    return [
        (r["reel_id"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT reel_id, payload FROM events WHERE type = ? ORDER BY id", (type,)
        )
    ]


def test_reels_queued_once_and_rerun_idempotent(conn):
    items = [
        item("c1", "https://www.instagram.com/reel/AAA/?x=1"),
        item("c2", "https://example.com"),
    ]
    r = run(conn, items)
    assert (r.new_reel, r.not_instagram) == (1, 1)
    assert rows(conn) == {"ig_AAA": ("queued", "https://www.instagram.com/p/AAA/")}
    assert events(conn, "reel_queued") == [
        ("ig_AAA", {"title": "t", "source": "ig_export"})
    ]

    r = run(conn, items)
    assert (r.new_reel, r.existing) == (0, 1)
    assert len(events(conn, "reel_queued")) == 1


def test_same_shortcode_other_card_logged_once(conn):
    items = [
        item("c1", "https://www.instagram.com/reel/AAA/"),
        item("c2", "https://www.instagram.com/p/AAA/?igsh=z"),
    ]
    r = run(conn, items)
    assert (r.new_reel, r.duplicates) == (1, 1)
    run(conn, items)
    assert list(rows(conn)) == ["ig_AAA"]
    assert events(conn, "source_duplicate") == [("ig_AAA", {"source_id": "c2"})]


def test_posts_probed_photo_queued_not_reprobed(conn):
    items = [
        item("c1", "https://www.instagram.com/p/VID/"),
        item("c2", "https://www.instagram.com/p/PIC/"),
        item("c3", "https://www.instagram.com/reel/REELP/"),
    ]
    probe = FakeProbe({"VID": True, "PIC": False})
    r = run(conn, items, probe)
    assert (r.new_reel, r.new_post, r.new_photo) == (1, 1, 1)
    assert probe.calls == ["VID", "PIC"]  # /reel/ is never probed
    assert rows(conn)["ig_PIC"][0] == "queued"  # photo posts are curated too
    assert events(conn, "reel_skipped") == []

    probe2 = FakeProbe({})
    run(conn, items, probe2)
    assert probe2.calls == []


def test_probe_stops_after_two_failures(conn):
    items = [item(f"c{i}", f"https://www.instagram.com/p/P{i}/") for i in range(4)]
    err = download.IgError("rate limit", transient=True)
    probe = FakeProbe({"P0": err, "P1": err, "P2": True, "P3": True})
    r = run(conn, items, probe)
    assert probe.calls == ["P0", "P1"]
    assert (r.deferred, r.probe_stopped) == (4, "rate limit")
    assert rows(conn) == {}

    r = run(conn, items, FakeProbe({f"P{i}": True for i in range(4)}))
    assert r.new_post == 4


def test_probe_limit_per_run(conn):
    items = [item(f"c{i}", f"https://www.instagram.com/p/P{i}/") for i in range(3)]
    probe = FakeProbe({f"P{i}": True for i in range(3)})
    r = run(conn, items, probe, max_probes=2)
    assert (r.new_post, r.deferred) == (2, 1)


def test_limit_new_inserts(conn):
    items = [item(f"c{i}", f"https://www.instagram.com/reel/R{i}/") for i in range(3)]
    r = run(conn, items, limit=2)
    assert r.new_reel == 2
    assert sorted(rows(conn)) == ["ig_R0", "ig_R1"]


# real yt-dlp message for a deleted/private post (DaNUKQ1EW3B)
DEAD = (
    "ERROR: [Instagram] DaNUKQ1EW3B: Instagram sent an empty media response."
    " Check if this post is accessible in your browser without being logged-in."
    " If it is not, then use --cookies-from-browser or --cookies for the authentication."
)


def test_classify_probe_errors(monkeypatch):
    import yt_dlp

    def boom(msg):
        class Y:
            def __init__(self, _):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, *a, **k):
                raise yt_dlp.utils.DownloadError(msg)

        return Y

    for msg, transient in [
        (DEAD, False),
        (
            "Requested content is not available, rate-limit reached or login required",
            True,
        ),
    ]:
        monkeypatch.setattr(yt_dlp, "YoutubeDL", boom(msg))
        with pytest.raises(download.IgError) as e:
            download.probe("https://www.instagram.com/p/X/")
        assert e.value.transient is transient
    monkeypatch.setattr(yt_dlp, "YoutubeDL", boom("There is no video in this post"))
    assert download.probe("https://www.instagram.com/p/X/") is False


def test_dead_post_skipped_after_three_runs(conn):
    items = [item("c1", "https://www.instagram.com/p/DEAD/")]
    dead = download.IgError(DEAD, transient=False)
    for _ in range(2):
        r = run(conn, items, FakeProbe({"DEAD": dead}))
        assert (r.deferred, r.unreachable) == (1, 0)
    assert rows(conn) == {}

    r = run(conn, items, FakeProbe({"DEAD": dead}))
    assert (r.deferred, r.unreachable) == (0, 1)
    assert rows(conn)["ig_DEAD"][0] == "skipped"
    assert events(conn, "reel_skipped")[-1][1]["reason"] == "unreachable"
    assert len(events(conn, "probe_failed")) == 3

    probe = FakeProbe({})
    run(conn, items, probe)
    assert probe.calls == []


def test_transient_failures_never_skip(conn):
    items = [item("c1", "https://www.instagram.com/p/RL/")]
    err = download.IgError("rate-limit reached", transient=True)
    for _ in range(5):
        run(conn, items, FakeProbe({"RL": err}))
    assert rows(conn) == {}
    assert events(conn, "probe_failed") == []


def test_tags_saved(conn):
    it = item("c1", "https://www.instagram.com/reel/AAA/")
    it.tags = ["food", "travel"]
    run(conn, [it])
    q = "SELECT source_tags FROM reels WHERE id = 'ig_AAA'"
    assert json.loads(conn.execute(q).fetchone()[0]) == ["food", "travel"]


def test_urls_file_skips_comments_and_dedups_by_shortcode(conn, tmp_path):
    from prism.sources.urls import UrlsSource

    f = tmp_path / "reels.txt"
    f.write_text(
        "# saved reels\n\nhttps://www.instagram.com/reel/AAA/?igsh=x\n"
        "  https://www.instagram.com/reel/AAA/  \nhttps://example.com/a\n"
    )
    items = UrlsSource(f).list_new()
    assert [(i.source, i.source_id) for i in items] == [
        ("urls", "AAA"), ("urls", "AAA"), ("urls", "https://example.com/a"),
    ]  # fmt: skip
    r = run(conn, items)
    assert (r.new_reel, r.existing, r.not_instagram) == (1, 1, 1)
    assert rows(conn) == {"ig_AAA": ("queued", "https://www.instagram.com/p/AAA/")}
