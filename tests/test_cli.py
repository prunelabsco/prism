import pytest
from typer.testing import CliRunner

from prism import config, db
from prism.cli import app

runner = CliRunner()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = config.settings.model_copy(
        update={"paths": config.Paths(data_dir=tmp_path / "data", nexus_inbox=tmp_path)}
    )
    monkeypatch.setattr(config, "settings", s)
    return s


def test_status_before_init_refused(settings):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1
    assert "Belum di-init" in r.output
    assert not settings.db_path.exists()


def test_init_idempotent(settings):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "Database dibuat" in r.output
    assert settings.reels_dir.is_dir() and settings.logs_dir.is_dir()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "Sudah di-init, skema v2" in r.output


def test_status_counts_and_categories(settings):
    runner.invoke(app, ["init"])
    conn = db.connect(settings.db_path)
    with conn:
        conn.executemany(
            "INSERT INTO reels (id, source, source_id, url, status, attempts)"
            " VALUES (?, 'mymind', ?, 'u', ?, ?)",
            [
                ("a", "1", "ready", 0),
                ("b", "2", "failed", 1),
                ("c", "3", "failed", db.MAX_ATTEMPTS),
            ],
        )
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES ('baru', 'Baru', 'd', 'proposed', 'sering muncul')"
        )
        conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail,"
            " why_relevant, confidence) VALUES ('a', 'ide-project', 't', 'd', 'w', 'tinggi')"
        )

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    out = r.output
    assert out.startswith("Usulan kategori")
    assert "baru (Baru): sering muncul" in out
    assert "ready" in out and "queued" not in out
    assert "(1 retry otomatis, 1 perlu `prism retry`)" in out
    assert "Ide Project: 1 ide" in out and "Konten YT AI: 0 ide" in out
    assert "Baru: " not in out.split("Kategori aktif:")[1]


def _seed_reels(settings, rows):
    runner.invoke(app, ["init"])
    conn = db.connect(settings.db_path)
    with conn:
        conn.executemany(
            "INSERT INTO reels (id, source, source_id, url, status, attempts, stage_error)"
            " VALUES (?, 'mymind', ?, 'u', ?, ?, ?)",
            [
                (i, i, st, n, "downloading: x" if st == "failed" else None)
                for i, st, n in rows
            ],
        )
    return conn


def _status(conn, reel_id):
    return conn.execute(
        "SELECT status, attempts FROM reels WHERE id = ?", (reel_id,)
    ).fetchone()


def test_retry_one_reel(settings):
    conn = _seed_reels(settings, [("a", "failed", db.MAX_ATTEMPTS), ("b", "failed", 3)])
    r = runner.invoke(app, ["retry", "a"])
    assert (
        r.exit_code == 0
        and "1 reel di-retry (1 ke antrian, 0 ke siap dikurasi)" in r.output
    )
    assert tuple(_status(conn, "a")) == ("queued", db.MAX_ATTEMPTS + 1)
    assert _status(conn, "b")["status"] == "failed"
    ev = conn.execute("SELECT type FROM events WHERE reel_id = 'a'").fetchone()
    assert ev["type"] == "reel_retried"


@pytest.mark.parametrize(
    "args", [["retry", "a"], ["retry", "nope"], ["retry"], ["retry", "a", "--all"]]
)
def test_retry_refused(settings, args):
    conn = _seed_reels(settings, [("a", "ready", 0)])
    r = runner.invoke(app, args)
    assert r.exit_code == 1
    assert _status(conn, "a")["status"] == "ready"


def test_retry_all(settings):
    conn = _seed_reels(
        settings,
        [("a", "failed", 1), ("b", "failed", db.MAX_ATTEMPTS), ("c", "ready", 0)],
    )
    r = runner.invoke(app, ["retry", "--all"])
    assert r.exit_code == 0 and "2 reel di-retry (2 ke antrian" in r.output
    assert [_status(conn, i)["status"] for i in "abc"] == ["queued", "queued", "ready"]


def test_retry_photos_only_skipped_photo_posts(settings):
    conn = _seed_reels(
        settings, [("pic", "skipped", 0), ("dead", "skipped", 0), ("f", "failed", 1)]
    )
    with conn:
        db.log_event(conn, "reel_skipped", "pic", reason="bukan_video")
        db.log_event(conn, "reel_skipped", "dead", reason="tidak_bisa_diakses")
    r = runner.invoke(app, ["retry", "--photos"])
    assert r.exit_code == 0 and "1 post foto ke antrian" in r.output
    assert tuple(_status(conn, "pic")) == ("queued", 0)  # attempts tidak bertambah
    assert [_status(conn, i)["status"] for i in ("dead", "f")] == ["skipped", "failed"]
    assert runner.invoke(app, ["retry", "--photos"]).output.startswith("0 post foto")


def test_next_and_commit(settings, tmp_path):
    conn = _seed_reels(settings, [("a", "ready", 0)])
    r = runner.invoke(app, ["next"])
    assert r.exit_code == 0 and r.output.strip().endswith("a/packet.json")
    assert runner.invoke(app, ["next"]).output.strip() == "Tidak ada reel ready."

    bad = tmp_path / "bad.json"
    bad.write_text('{"reel_id": "a"}')
    r = runner.invoke(app, ["commit", "a", str(bad)])
    assert r.exit_code == 1 and "summary" in r.output

    good = tmp_path / "good.json"
    good.write_text('{"reel_id": "a", "summary": "s", "card_line": "c", "ideas": []}')
    r = runner.invoke(app, ["commit", "a", str(good)])
    assert r.exit_code == 0 and "a dikurasi: 0 ide" in r.output
    assert (settings.reels_dir / "a" / "curation.json").exists()  # bukan data asli
    assert _status(conn, "a")["status"] == "curated"


def test_commit_fail_flag(settings):
    conn = _seed_reels(settings, [("a", "curating", 0)])
    assert runner.invoke(app, ["commit", "a"]).exit_code == 1
    r = runner.invoke(app, ["commit", "a", "--fail", "json salah"])
    assert r.exit_code == 0 and "ditandai gagal" in r.output
    assert _status(conn, "a")["status"] == "failed"


def test_category_flow(settings):
    conn = _seed_reels(settings, [("a", "curated", 0)])
    with conn:
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES ('desain', 'Desain', 'd', 'proposed', 'sering')"
        )
        conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES ('a', 'desain', 'judul', 'd', 'w', 'sedang')"
        )
    out = runner.invoke(app, ["category", "list"]).output
    assert out.splitlines()[0].split()[:3] == ["desain", "proposed", "1"]

    r = runner.invoke(app, ["category", "reject", "desain"])
    assert r.exit_code == 0 and "1 ide jadi perlu kategori" in r.output
    assert runner.invoke(app, ["category", "approve", "desain"]).exit_code == 1
    assert "1 ide perlu kategori" in runner.invoke(app, ["status"]).output

    r = runner.invoke(app, ["recategorize"])
    assert "a  judul" in r.output
    assert runner.invoke(app, ["recategorize", "1"]).exit_code == 1
    r = runner.invoke(app, ["recategorize", "1", "ide-project"])
    assert r.exit_code == 0 and "pindah ke ide-project" in r.output
    assert "Tidak ada ide" in runner.invoke(app, ["recategorize"]).output
