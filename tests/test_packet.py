import json

import pytest

from prism import db, packet


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "prism.db")
    db.migrate(c)
    return c


@pytest.fixture
def reel(conn, tmp_path):
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, title, user_note, status)"
            " VALUES ('ig_A', 'mymind', 'm1', 'https://www.instagram.com/p/A/',"
            " 'judul', 'catatan', 'transcribing')"
        )
    out = tmp_path / "ig_A"
    out.mkdir()
    (out / "meta.json").write_text(
        json.dumps({"duration_s": 39.2, "width": 360, "caption": "cap"})
    )
    (out / "frames.json").write_text(json.dumps([{"path": "frames/0001.jpg", "t": 0}]))
    (out / "transcript.json").write_text(
        json.dumps(
            {
                "language": "en",
                "text": "halo",
                "segments": [{"start": 0, "end": 1, "text": "halo"}],
                "has_speech": True,
            }
        )
    )
    return out


def test_build_fields(conn, reel):
    p = packet.build(conn, "ig_A", reel)
    assert json.loads((reel / "packet.json").read_text()) == p
    assert {k: p[k] for k in ("reel_id", "url", "title", "user_note")} == {
        "reel_id": "ig_A",
        "url": "https://www.instagram.com/p/A/",
        "title": "judul",
        "user_note": "catatan",
    }
    assert p["caption"] == "cap" and p["duration_s"] == 39.2
    assert p["has_speech"] is True
    assert p["transcript"] == [{"start": 0, "end": 1, "text": "halo"}]
    assert p["frames"] == [{"path": "frames/0001.jpg", "t": 0}]
    assert [c["slug"] for c in p["categories"]][:2] == [
        "konten-yt-ai",
        "config-claude-code",
    ]
    assert p["recent_titles"] == []
    assert p["collections"] == []  # tag mymind tidak ikut


def test_ig_export_collections(conn, reel):
    with conn:
        conn.execute(
            "UPDATE reels SET source = 'ig_export', source_tags = ? WHERE id = 'ig_A'",
            (json.dumps(["food", "design"]),),
        )
    assert packet.build(conn, "ig_A", reel)["collections"] == ["food", "design"]


def test_categories_with_proposed_and_recent(conn, reel):
    with conn:
        conn.executemany(
            "INSERT INTO categories (slug, name, description, status, sort_order)"
            " VALUES (?, ?, 'd', ?, 0)",
            [("usul", "Usul", "proposed"), ("tolak", "Tolak", "rejected")],
        )
        for i in range(packet.RECENT_LIMIT + 2):
            conn.execute(
                "INSERT INTO reels (id, source, source_id, url, title, card_line,"
                " status, updated_at) VALUES (?, 'mymind', ?, 'u', ?, ?, ?,"
                " datetime('2026-01-01', ? || ' minutes'))",
                (
                    f"r{i:02d}",
                    f"s{i}",
                    f"judul {i}",
                    None if i % 2 else f"kartu {i}",
                    "exported" if i == 0 else "curated",
                    i,
                ),
            )
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status)"
            " VALUES ('ready1', 'mymind', 'x', 'u', 'ready')"
        )
    p = packet.build(conn, "ig_A", reel)
    slugs = [c["slug"] for c in p["categories"]]
    assert slugs[-1] == "usul" and "tolak" not in slugs  # proposed paling bawah
    recent = p["recent_titles"]
    assert len(recent) == packet.RECENT_LIMIT
    assert recent[0] == {
        "reel_id": "r31",
        "title": "judul 31",
    }  # terbaru, tanpa card_line
    assert recent[1] == {"reel_id": "r30", "title": "kartu 30"}
    assert "ready1" not in [r["reel_id"] for r in recent]


def test_missing_file_raises(conn, reel):
    (reel / "transcript.json").unlink()
    with pytest.raises(OSError):
        packet.build(conn, "ig_A", reel)
