import json

import pytest

from prism import config, curation, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


def add(conn, reel_id, status="ready", updated="2026-01-01"):
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status, updated_at)"
            " VALUES (?, 'ig_export', ?, 'u', ?, ?)",
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


def idea(category="projects"):
    return {
        "title": "t",
        "detail": "d",
        "why_relevant": "w",
        "category": category,
        "confidence": "high",
    }


def write(tmp_path, reel_id="ig_A", **changes):
    data = {
        "reel_id": reel_id,
        "summary": "reel content",
        "card_line": "card",
        "ideas": [idea(), idea("learn"), idea()],
        "evidence": [{"label": "seen", "text": "x", "at_s": 1.5}],
        "next_actions": ["check the repo"],
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
    assert r.categories == ["projects", "learn"]
    assert status(conn, "ig_A") == "curated"
    assert (count(conn, "ideas"), count(conn, "evidence")) == (3, 1)
    row = conn.execute("SELECT summary, card_line FROM reels").fetchone()
    assert tuple(row) == ("reel content", "card")
    assert events(conn, "ig_A")[-1] == (
        "reel_curated",
        {"card_line": "card", "categories": r.categories, "idea_count": 3},
    )
    saved = json.loads((tmp_path / "ig_A" / "curation.json").read_text())
    assert saved["next_actions"] == ["check the repo"]


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
    prop = {"slug": "design-ui", "name": "Design UI", "description": "d", "reason": "r"}
    p = write(tmp_path, ideas=[idea("design-ui")], proposed_category=prop)
    r = curation.commit(conn, "ig_A", p, tmp_path)
    assert r.proposed == "design-ui"
    row = conn.execute(
        "SELECT status, proposed_by FROM categories WHERE slug = 'design-ui'"
    ).fetchone()
    assert tuple(row) == ("proposed", "ig_A")
    types = [t for t, _ in events(conn, "ig_A")]
    assert types == ["category_proposed", "reel_curated"]


@pytest.mark.parametrize(
    "changes, msg",
    [
        ({"card_line": "x" * 71}, "card_line"),
        ({"reel_id": "ig_Z"}, "does not match argument"),
        (
            {"ideas": [], "duplicate_of": "ig_A", "duplicate_reason": "r"},
            "duplicate_of",
        ),
        (
            {"ideas": [], "duplicate_of": "ig_X", "duplicate_reason": "r"},
            "duplicate_of",
        ),
        ({"ideas": [idea("bogus")]}, "bogus"),
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
    with pytest.raises(curation.CurationError, match="does not exist"):
        curation.commit(conn, "ig_Q", write(tmp_path), tmp_path)


def test_fail_then_auto_retry_target(conn):
    add(conn, "ig_A", status="curating")
    curation.fail(conn, "ig_A", "bad json 3x")
    assert status(conn, "ig_A") == "failed"
    assert db.retry(conn, "ig_A") == "ready"


def add_category(conn, slug, status="proposed"):
    with conn:
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES (?, ?, 'd', ?, 'reason')",
            (slug, slug.title(), status),
        )


def add_idea(conn, category, reel_id="ig_A"):
    with conn:
        return conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES (?, ?, 't', 'd', 'w', 'medium')",
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
    add_category(conn, "design")
    curation.approve(conn, "design")
    assert cat_status(conn, "design") == "approved"
    assert last_event(conn) == (None, "category_approved", {"slug": "design"})


def test_reject_ideas_become_uncategorized(conn):
    add(conn, "ig_A", status="curated")
    add_category(conn, "design")
    a, b = add_idea(conn, "design"), add_idea(conn, "projects")
    assert curation.reject(conn, "design") == 1
    assert cat_status(conn, "design") == "rejected"
    assert [r["id"] for r in curation.uncategorized(conn)] == [a]
    assert last_event(conn) == (None, "category_rejected", {"slug": "design"})
    assert b not in [r["id"] for r in curation.uncategorized(conn)]


@pytest.mark.parametrize("fn", [curation.approve, curation.reject])
@pytest.mark.parametrize(
    "slug, msg",
    [("projects", "is core"), ("nope", "is rejected"), ("x", "does not exist")],
)
def test_decide_only_proposed(conn, fn, slug, msg):
    add_category(conn, "nope", status="rejected")
    with pytest.raises(curation.CurationError, match=msg):
        fn(conn, slug)


def test_recategorize(conn):
    add(conn, "ig_A", status="curated")
    add_category(conn, "design")
    i = add_idea(conn, "design")
    curation.reject(conn, "design")
    curation.recategorize(conn, i, "projects")
    assert curation.uncategorized(conn) == []
    assert last_event(conn) == (
        "ig_A",
        "idea_recategorized",
        {"idea_id": i, "slug": "projects"},
    )


@pytest.mark.parametrize(
    "target, msg",
    [("maybe", "is proposed"), ("design", "is rejected"), ("x", "does not exist")],
)
def test_recategorize_only_to_active(conn, target, msg):
    add(conn, "ig_A", status="curated")
    add_category(conn, "design")
    add_category(conn, "maybe")
    i = add_idea(conn, "design")
    curation.reject(conn, "design")
    with pytest.raises(curation.CurationError, match=msg):
        curation.recategorize(conn, i, target)


def test_recategorize_rejects_categorized_or_missing(conn):
    add(conn, "ig_A", status="curated")
    i = add_idea(conn, "projects")
    with pytest.raises(curation.CurationError, match="is already in"):
        curation.recategorize(conn, i, "recipes")
    with pytest.raises(curation.CurationError, match="does not exist"):
        curation.recategorize(conn, 999, "recipes")


def test_categories_order(conn):
    add_category(conn, "nope", status="rejected")
    add_category(conn, "maybe")
    statuses = [r["status"] for r in curation.categories(conn)]
    assert statuses == ["proposed"] + ["core"] * 5 + ["rejected"]


def test_next_refreshes_recent_titles(conn, tmp_path):
    add(conn, "ig_A")
    add(conn, "ig_B", status="curated")
    d = tmp_path / "ig_A"
    d.mkdir()
    (d / "packet.json").write_text(json.dumps({"reel_id": "ig_A", "recent_titles": []}))
    path = curation.next_packet(conn, tmp_path)
    assert json.loads(path.read_text())["recent_titles"][0]["reel_id"] == "ig_B"


def test_next_refreshes_categories(conn, tmp_path):
    add(conn, "ig_A")
    conn.execute(
        "INSERT INTO categories (slug, name, description, status)"
        " VALUES ('home-decor', 'Home decor', 'Decor ideas', 'proposed')"
    )
    d = tmp_path / "ig_A"
    d.mkdir()
    (d / "packet.json").write_text(json.dumps({"reel_id": "ig_A", "categories": []}))
    path = curation.next_packet(conn, tmp_path)
    slugs = [c["slug"] for c in json.loads(path.read_text())["categories"]]
    assert "home-decor" in slugs


def test_add_category(conn):
    curation.add(conn, "home-decor", "Home Decor")
    row = conn.execute(
        "SELECT name, description, status FROM categories WHERE slug = 'home-decor'"
    ).fetchone()
    assert tuple(row) == ("Home Decor", "Home Decor", "approved")


@pytest.mark.parametrize("slug", ["Home Decor", "food🍴", "-x", ""])
def test_add_rejects_bad_slug(conn, slug):
    with pytest.raises(curation.CurationError, match="not a valid slug"):
        curation.add(conn, slug, "x")


def test_add_refuses_existing_but_revives_rejected(conn):
    with pytest.raises(curation.CurationError, match="already exists: core"):
        curation.add(conn, "recipes", "x")
    curation.add(conn, "design", "Design")
    conn.execute("UPDATE categories SET status = 'rejected' WHERE slug = 'design'")
    curation.add(conn, "design", "Design 2")
    row = conn.execute(
        "SELECT name, status FROM categories WHERE slug = 'design'"
    ).fetchone()
    assert tuple(row) == ("Design 2", "approved")


def _mapped(monkeypatch, mapping):
    monkeypatch.setattr(
        config, "settings", config.settings.model_copy(update={"collections": mapping})
    )


def test_next_applies_mapping_made_after_process(conn, tmp_path, monkeypatch):
    _mapped(monkeypatch, {"makan": "recipes", "resto": "places", "off": "nope"})
    add(conn, "ig_A", "ready")
    d = tmp_path / "ig_A"
    d.mkdir()
    (d / "packet.json").write_text(
        json.dumps({"reel_id": "ig_A", "collections": ["makan", "off", "resto", "x"]})
    )
    data = json.loads(curation.next_packet(conn, tmp_path).read_text())
    assert data["collection_categories"] == ["recipes", "places"]


def test_collections_overview(conn, monkeypatch):
    _mapped(monkeypatch, {"makan": "recipes", "off": "nope"})
    for i, tags in enumerate((["makan"], ["makan", "off"], None)):
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status, source_tags)"
            " VALUES (?, 'ig_export', ?, 'u', 'queued', ?)",
            (f"r{i}", f"r{i}", json.dumps(tags) if tags else None),
        )
    rows, loose = curation.collections(conn)
    assert loose == 1
    assert rows == [
        {"name": "makan", "reels": 2, "slug": "recipes", "active": True},
        {"name": "off", "reels": 1, "slug": "nope", "active": False},
    ]
