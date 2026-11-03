import json

import pytest

from prism import db, sync


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


def exported(conn, tmp_path, rid="ig_A"):
    inbox = tmp_path / "vault" / "00-Inbox" / "Prism"
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / f"{rid}.md"
    p.write_text(f"---\nprism_id: {rid}\nstatus: baru\n---\nisi\n")
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status, note_path)"
            " VALUES (?, 'mymind', ?, 'u', 'exported', ?)",
            (rid, rid, str(p)),
        )
    return p, tmp_path / "vault"


def status_events(conn):
    return [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT payload FROM events WHERE type = 'user_status_changed'"
        )
    ]


def test_status_change_synced_once(conn, tmp_path):
    p, vault = exported(conn, tmp_path)
    assert sync.run(conn, vault).changed == []
    p.write_text(p.read_text().replace("status: baru", "status: dipilih"))
    assert sync.run(conn, vault).changed == [("ig_A", "baru", "dipilih")]
    assert sync.run(conn, vault).changed == []  # idempoten
    assert status_events(conn) == [{"from": "baru", "to": "dipilih"}]
    assert [r["title"] for r in db.picked_ideas(conn)] == []  # belum ada ide


def test_moved_note_found_by_prism_id(conn, tmp_path):
    p, vault = exported(conn, tmp_path)
    dest = vault / "05-AI" / "prompts"
    dest.mkdir(parents=True)
    p.rename(dest / "pindah.md")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "x.md").write_text(
        "---\nprism_id: ig_A\n---\n"
    )  # harus diabaikan
    r = sync.run(conn, vault)
    assert r.moved == ["ig_A"]
    row = conn.execute("SELECT note_path FROM reels WHERE id = 'ig_A'").fetchone()
    assert row[0] == str(dest / "pindah.md")


def test_missing_and_invalid_status_reported_not_applied(conn, tmp_path):
    p, vault = exported(conn, tmp_path, "ig_A")
    q, _ = exported(conn, tmp_path, "ig_B")
    p.write_text(p.read_text().replace("status: baru", "status: ngawur"))
    q.unlink()
    r = sync.run(conn, vault)
    assert r.invalid == [("ig_A", "ngawur")] and r.missing == ["ig_B"]
    assert status_events(conn) == []


def test_picked_ideas_grouped_by_category(conn, tmp_path):
    exported(conn, tmp_path)
    with conn:
        conn.execute("UPDATE reels SET user_status = 'dipilih'")
        conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES ('ig_A', 'ide-project', 'T', 'd', 'w', 'tinggi')"
        )
    (row,) = db.picked_ideas(conn)
    assert (row["category"], row["title"]) == ("Ide Project", "T")
