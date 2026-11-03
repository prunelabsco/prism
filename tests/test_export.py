import json

import pytest

from prism import db, export


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


@pytest.fixture
def dirs(tmp_path):
    inbox, reels = tmp_path / "Prism", tmp_path / "reels"
    return inbox, reels


def curated(conn, reels_dir, rid, note="", tags=(), ideas=1, actions=()):
    """Reel `curated` lengkap dengan ide, evidence, event, dan curation.json."""
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, user_note, source_tags,"
            " summary, card_line, status) VALUES (?, 'mymind', ?, ?, ?, ?, 'ringkas',"
            " 'kartu', 'curated')",
            (rid, f"card_{rid}", f"https://www.instagram.com/p/{rid}/", note,
             json.dumps(list(tags))),
        )  # fmt: skip
        for n in range(ideas):
            conn.execute(
                "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
                " confidence) VALUES (?, 'config-claude-code', ?, 'detail', 'relevan', 'sedang')",
                (rid, f"Ide {rid} {n}"),
            )
        conn.execute(
            "INSERT INTO evidence (reel_id, label, text, at_s) VALUES (?, 'terlihat', 'x', 72)",
            (rid,),
        )
        db.log_event(conn, "reel_curated", rid)
    (reels_dir / rid).mkdir(parents=True, exist_ok=True)
    (reels_dir / rid / "curation.json").write_text(
        json.dumps({"next_actions": list(actions)})
    )


def note(inbox, rid):
    (p,) = [p for p in inbox.glob("*.md") if f"prism_id: {rid}\n" in p.read_text()]
    return p


def test_export_writes_template_and_marks_exported(conn, dirs):
    inbox, reels = dirs
    curated(conn, reels, "ig_A", note="baris satu\nbaris dua: ok", tags=["ai", "tools"],
            actions=["cek repo"])  # fmt: skip
    r = export.run(conn, inbox, reels)
    assert (r.written, r.skipped) == (1, [])

    p = note(inbox, "ig_A")
    text = p.read_text()
    fm = export.read_frontmatter(text)
    assert fm["mymind_id"] == "card_ig_A"
    assert fm["user_note"] == "baris satu\nbaris dua: ok"  # multi-baris selamat
    assert fm["mymind_tags"] == ["ai", "tools"]
    assert fm["kategori"] == ["Config Claude Code"]
    assert (fm["status"], fm["confidence"]) == ("baru", "sedang")
    assert "- **Terlihat** (1:12): x" in text and "- [ ] cek repo" in text
    assert text.rstrip().endswith(
        "<!-- prism: area bebas, tidak pernah disentuh mesin -->"
    )

    row = conn.execute("SELECT * FROM reels WHERE id = 'ig_A'").fetchone()
    assert (row["status"], row["nexus_path"]) == ("exported", str(p))
    assert row["export_hash"] == export.sha256(p.read_bytes())


def test_rerun_unchanged_and_never_overwrites_edits(conn, dirs):
    inbox, reels = dirs
    for rid in ("ig_A", "ig_B", "ig_C"):
        curated(conn, reels, rid)
    assert export.run(conn, inbox, reels).written == 3
    assert export.run(conn, inbox, reels).unchanged == 3  # idempoten

    a, b = note(inbox, "ig_A"), note(inbox, "ig_B")
    a.write_text(a.read_text().replace("status: baru", "status: dipilih"))
    b.write_text(b.read_text() + "catatan pribadi Rakha\n")

    with conn:  # kategori berubah di DB: ig_C boleh diperbarui, A dan B tidak boleh
        conn.execute("UPDATE ideas SET category_slug = 'ide-project'")
    r = export.run(conn, inbox, reels)
    assert (r.rewritten, r.unchanged, len(r.skipped)) == (1, 0, 2)
    assert dict(r.skipped) == {"ig_A": "isi sudah diedit", "ig_B": "isi sudah diedit"}
    assert "status: dipilih" in a.read_text()
    assert "catatan pribadi Rakha" in b.read_text()
    assert "Ide Project" in note(inbox, "ig_C").read_text()


def test_moved_note_skipped_and_not_recreated(conn, dirs):
    inbox, reels = dirs
    curated(conn, reels, "ig_A")
    export.run(conn, inbox, reels)
    note(inbox, "ig_A").rename(inbox.parent / "dipindah.md")
    r = export.run(conn, inbox, reels)
    assert r.skipped[0][0] == "ig_A" and "prism sync" in r.skipped[0][1]
    assert list(inbox.glob("*.md")) == []


def test_filename_collision_gets_reel_id_suffix(conn, dirs):
    inbox, reels = dirs
    curated(conn, reels, "ig_A")
    curated(conn, reels, "ig_B")
    with conn:
        conn.execute("UPDATE ideas SET title = 'Sama: judul/ide?'")
    assert export.run(conn, inbox, reels).written == 2
    names = sorted(p.name for p in inbox.glob("*.md"))
    assert len(names) == 2 and all("/" not in n and "?" not in n for n in names)
    assert any("ig_B" in n for n in names) or any("ig_A" in n for n in names)


def test_zero_ideas_note_still_valid(conn, dirs):
    inbox, reels = dirs
    curated(conn, reels, "ig_A", ideas=0)
    export.run(conn, inbox, reels)
    fm = export.read_frontmatter(note(inbox, "ig_A").read_text())
    assert fm["confidence"] is None and fm["kategori"] == []
