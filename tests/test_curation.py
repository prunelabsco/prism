import json

import pytest

from prism import curation, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


def add(conn, reel_id, status="ready", updated="2026-01-01"):
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status, updated_at)"
            " VALUES (?, 'mymind', ?, 'u', ?, ?)",
            (reel_id, reel_id, status, updated),
        )


def status(conn, reel_id):
    return conn.execute("SELECT status FROM reels WHERE id = ?", (reel_id,)).fetchone()[
        0
    ]


def events(conn, reel_id):
    return [
        (r["type"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT type, payload FROM events WHERE reel_id = ? ORDER BY id", (reel_id,)
        )
    ]


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def idea(category="ide-project"):
    return {
        "title": "t",
        "detail": "d",
        "why_relevant": "w",
        "category": category,
        "confidence": "tinggi",
    }


def write(tmp_path, reel_id="ig_A", **changes):
    data = {
        "reel_id": reel_id,
        "summary": "isi reel",
        "card_line": "kartu",
        "ideas": [idea(), idea("config-claude-code"), idea()],
        "evidence": [{"label": "terlihat", "text": "x", "at_s": 1.5}],
        "next_actions": ["cek repo"],
        **changes,
    }
    p = tmp_path / "in.json"
    p.write_text(json.dumps(data))
    return p


def test_next_oldest_ready_and_resets_stale(conn, tmp_path):
    add(conn, "new", updated="2026-01-03")
    add(conn, "old", updated="2026-01-02")
    add(conn, "stale", status="curating", updated="2026-01-01")
    add(conn, "fresh", status="curating", updated="9999-01-01")

    assert curation.next_packet(conn, tmp_path) == tmp_path / "old" / "packet.json"
    assert status(conn, "old") == "curating"
    assert status(conn, "stale") == "ready"
    assert events(conn, "stale") == [("curation_reset", {})]
    assert status(conn, "fresh") == "curating"


def test_next_empty(conn, tmp_path):
    assert curation.next_packet(conn, tmp_path) is None


def test_commit_curated(conn, tmp_path):
    add(conn, "ig_A", status="curating")
    r = curation.commit(conn, "ig_A", write(tmp_path), tmp_path)
    assert (r.status, r.idea_count) == ("curated", 3)
    assert r.categories == ["ide-project", "config-claude-code"]
    assert status(conn, "ig_A") == "curated"
    assert (count(conn, "ideas"), count(conn, "evidence")) == (3, 1)
    row = conn.execute("SELECT summary, card_line FROM reels").fetchone()
    assert tuple(row) == ("isi reel", "kartu")
    assert events(conn, "ig_A")[-1] == (
        "reel_curated",
        {"card_line": "kartu", "categories": r.categories, "idea_count": 3},
    )
    saved = json.loads((tmp_path / "ig_A" / "curation.json").read_text())
    assert saved["next_actions"] == ["cek repo"]


def test_commit_duplicate(conn, tmp_path):
    add(conn, "ig_B", status="curated")
    add(conn, "ig_A", status="curating")
    p = write(tmp_path, ideas=[], duplicate_of="ig_B", duplicate_reason="re-upload")
    r = curation.commit(conn, "ig_A", p, tmp_path)
    assert (r.status, r.duplicate_of) == ("duplicate", "ig_B")
    assert status(conn, "ig_A") == "duplicate"
    assert events(conn, "ig_A")[-1] == (
        "duplicate_detected",
        {"duplicate_of": "ig_B", "reason": "re-upload"},
    )


def test_commit_proposed_category(conn, tmp_path):
    add(conn, "ig_A", status="curating")
    prop = {"slug": "desain-ui", "name": "Desain UI", "description": "d", "reason": "r"}
    p = write(tmp_path, ideas=[idea("desain-ui")], proposed_category=prop)
    r = curation.commit(conn, "ig_A", p, tmp_path)
    assert r.proposed == "desain-ui"
    row = conn.execute(
        "SELECT status, proposed_by FROM categories WHERE slug = 'desain-ui'"
    ).fetchone()
    assert tuple(row) == ("proposed", "ig_A")
    types = [t for t, _ in events(conn, "ig_A")]
    assert types == ["category_proposed", "reel_curated"]


@pytest.mark.parametrize(
    "changes, msg",
    [
        ({"card_line": "x" * 71}, "card_line"),
        ({"reel_id": "ig_Z"}, "beda dengan argumen"),
        (
            {"ideas": [], "duplicate_of": "ig_A", "duplicate_reason": "r"},
            "duplicate_of",
        ),
        (
            {"ideas": [], "duplicate_of": "ig_X", "duplicate_reason": "r"},
            "duplicate_of",
        ),
        ({"ideas": [idea("ngawur")]}, "ngawur"),
    ],
)
def test_commit_invalid_no_side_effect(conn, tmp_path, changes, msg):
    add(conn, "ig_A", status="curating")
    before = count(conn, "events")
    with pytest.raises(curation.CurationError, match=msg):
        curation.commit(conn, "ig_A", write(tmp_path, **changes), tmp_path)
    assert status(conn, "ig_A") == "curating"
    assert (count(conn, "ideas"), count(conn, "evidence")) == (0, 0)
    assert count(conn, "events") == before
    assert not (tmp_path / "ig_A" / "curation.json").exists()


def test_commit_requires_curating(conn, tmp_path):
    add(conn, "ig_A", status="ready")
    with pytest.raises(curation.CurationError, match="prism next"):
        curation.commit(conn, "ig_A", write(tmp_path), tmp_path)
    with pytest.raises(curation.CurationError, match="tidak ada"):
        curation.commit(conn, "ig_Q", write(tmp_path), tmp_path)


def test_fail_then_auto_retry_target(conn):
    add(conn, "ig_A", status="curating")
    curation.fail(conn, "ig_A", "json salah 3x")
    assert status(conn, "ig_A") == "failed"
    assert db.retry(conn, "ig_A") == "ready"


def add_category(conn, slug, status="proposed"):
    with conn:
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES (?, ?, 'd', ?, 'alasan')",
            (slug, slug.title(), status),
        )


def add_idea(conn, category, reel_id="ig_A"):
    with conn:
        return conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES (?, ?, 't', 'd', 'w', 'sedang')",
            (reel_id, category),
        ).lastrowid


def cat_status(conn, slug):
    return conn.execute(
        "SELECT status FROM categories WHERE slug = ?", (slug,)
    ).fetchone()[0]


def last_event(conn):
    r = conn.execute(
        "SELECT reel_id, type, payload FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return r["reel_id"], r["type"], json.loads(r["payload"])


def test_approve(conn):
    add_category(conn, "desain")
    curation.approve(conn, "desain")
    assert cat_status(conn, "desain") == "approved"
    assert last_event(conn) == (None, "category_approved", {"slug": "desain"})


def test_reject_ideas_become_uncategorized(conn):
    add(conn, "ig_A", status="curated")
    add_category(conn, "desain")
    a, b = add_idea(conn, "desain"), add_idea(conn, "ide-project")
    assert curation.reject(conn, "desain") == 1
    assert cat_status(conn, "desain") == "rejected"
    assert [r["id"] for r in curation.uncategorized(conn)] == [a]
    assert last_event(conn) == (None, "category_rejected", {"slug": "desain"})
    assert b not in [r["id"] for r in curation.uncategorized(conn)]


@pytest.mark.parametrize("fn", [curation.approve, curation.reject])
@pytest.mark.parametrize(
    "slug, msg",
    [("ide-project", "status core"), ("tolak", "status rejected"), ("x", "tidak ada")],
)
def test_decide_only_proposed(conn, fn, slug, msg):
    add_category(conn, "tolak", status="rejected")
    with pytest.raises(curation.CurationError, match=msg):
        fn(conn, slug)


def test_recategorize(conn):
    add(conn, "ig_A", status="curated")
    add_category(conn, "desain")
    i = add_idea(conn, "desain")
    curation.reject(conn, "desain")
    curation.recategorize(conn, i, "ide-project")
    assert curation.uncategorized(conn) == []
    assert last_event(conn) == (
        "ig_A",
        "idea_recategorized",
        {"idea_id": i, "slug": "ide-project"},
    )


@pytest.mark.parametrize(
    "target, msg",
    [("usul", "status proposed"), ("desain", "status rejected"), ("x", "tidak ada")],
)
def test_recategorize_only_to_active(conn, target, msg):
    add(conn, "ig_A", status="curated")
    add_category(conn, "desain")
    add_category(conn, "usul")
    i = add_idea(conn, "desain")
    curation.reject(conn, "desain")
    with pytest.raises(curation.CurationError, match=msg):
        curation.recategorize(conn, i, target)


def test_recategorize_rejects_categorized_or_missing(conn):
    add(conn, "ig_A", status="curated")
    i = add_idea(conn, "ide-project")
    with pytest.raises(curation.CurationError, match="sudah di kategori"):
        curation.recategorize(conn, i, "konten-yt-ai")
    with pytest.raises(curation.CurationError, match="tidak ada"):
        curation.recategorize(conn, 999, "konten-yt-ai")


def test_categories_order(conn):
    add_category(conn, "tolak", status="rejected")
    add_category(conn, "usul")
    statuses = [r["status"] for r in curation.categories(conn)]
    assert statuses == ["proposed", "core", "core", "core", "rejected"]


def test_next_refreshes_recent_titles(conn, tmp_path):
    add(conn, "ig_A")
    add(conn, "ig_B", status="curated")
    d = tmp_path / "ig_A"
    d.mkdir()
    (d / "packet.json").write_text(json.dumps({"reel_id": "ig_A", "recent_titles": []}))
    path = curation.next_packet(conn, tmp_path)
    assert json.loads(path.read_text())["recent_titles"][0]["reel_id"] == "ig_B"
