import importlib.metadata
import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from prism import config, db
from prism.cli import app

runner = CliRunner()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = config.settings.model_copy(
        update={"paths": config.Paths(data_dir=tmp_path / "data", output_dir=tmp_path)}
    )
    monkeypatch.setattr(config, "settings", s)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "PROFILE_FILE", tmp_path / "profile.md")
    return s


def test_status_before_init_refused(settings):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1
    assert "Not initialized" in r.output
    assert not settings.db_path.exists()


def test_init_idempotent(settings):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "Database created" in r.output
    assert settings.reels_dir.is_dir() and settings.logs_dir.is_dir()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "Already initialized, schema v1" in r.output


def test_status_counts_and_categories(settings):
    runner.invoke(app, ["init"])
    conn = db.connect(settings.db_path)
    with conn:
        conn.executemany(
            "INSERT INTO reels (id, source, source_id, url, status, attempts)"
            " VALUES (?, 'ig_export', ?, 'u', ?, ?)",
            [
                ("a", "1", "ready", 0),
                ("b", "2", "failed", 1),
                ("c", "3", "failed", db.MAX_ATTEMPTS),
            ],
        )
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES ('fresh', 'Fresh', 'd', 'proposed', 'shows up often')"
        )
        conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail,"
            " why_relevant, confidence) VALUES ('a', 'projects', 't', 'd', 'w', 'high')"
        )

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0
    out = r.output
    assert out.startswith("Proposed categories")
    assert "fresh (Fresh): shows up often" in out
    assert "ready" in out and "queued" not in out
    assert "(1 auto-retry, 1 need `prism retry`)" in out
    assert "Projects: 1 ideas" in out and "Recipes: 0 ideas" in out
    assert "Fresh: " not in out.split("Active categories:")[1]


def _seed_reels(settings, rows):
    runner.invoke(app, ["init"])
    conn = db.connect(settings.db_path)
    with conn:
        conn.executemany(
            "INSERT INTO reels (id, source, source_id, url, status, attempts, stage_error)"
            " VALUES (?, 'ig_export', ?, 'u', ?, ?, ?)",
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
        r.exit_code == 0 and "1 reels retried (1 queued, 0 ready to curate)" in r.output
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
    assert r.exit_code == 0 and "2 reels retried (2 queued" in r.output
    assert [_status(conn, i)["status"] for i in "abc"] == ["queued", "queued", "ready"]


def test_retry_photos_only_skipped_photo_posts(settings):
    conn = _seed_reels(
        settings, [("pic", "skipped", 0), ("dead", "skipped", 0), ("f", "failed", 1)]
    )
    with conn:
        db.log_event(conn, "reel_skipped", "pic", reason="not_video")
        db.log_event(conn, "reel_skipped", "dead", reason="unreachable")
    r = runner.invoke(app, ["retry", "--photos"])
    assert r.exit_code == 0 and "1 photo posts queued" in r.output
    assert tuple(_status(conn, "pic")) == ("queued", 0)  # attempts not incremented
    assert [_status(conn, i)["status"] for i in ("dead", "f")] == ["skipped", "failed"]
    assert runner.invoke(app, ["retry", "--photos"]).output.startswith("0 photo posts")


def test_next_and_commit(settings, tmp_path):
    conn = _seed_reels(settings, [("a", "ready", 0)])
    r = runner.invoke(app, ["next"])
    assert r.exit_code == 0 and r.output.strip().endswith("a/packet.json")
    assert runner.invoke(app, ["next"]).output.strip() == "No ready reels."

    bad = tmp_path / "bad.json"
    bad.write_text('{"reel_id": "a"}')
    r = runner.invoke(app, ["commit", "a", str(bad)])
    assert r.exit_code == 1 and "summary" in r.output

    good = tmp_path / "good.json"
    good.write_text('{"reel_id": "a", "summary": "s", "card_line": "c", "ideas": []}')
    r = runner.invoke(app, ["commit", "a", str(good)])
    assert r.exit_code == 0 and "a curated: 0 ideas" in r.output
    assert (settings.reels_dir / "a" / "curation.json").exists()  # tmp, not real data
    assert _status(conn, "a")["status"] == "curated"


def test_commit_fail_flag(settings):
    conn = _seed_reels(settings, [("a", "curating", 0)])
    assert runner.invoke(app, ["commit", "a"]).exit_code == 1
    r = runner.invoke(app, ["commit", "a", "--fail", "bad json"])
    assert r.exit_code == 0 and "marked failed" in r.output
    assert _status(conn, "a")["status"] == "failed"


def test_category_flow(settings):
    conn = _seed_reels(settings, [("a", "curated", 0)])
    with conn:
        conn.execute(
            "INSERT INTO categories (slug, name, description, status, reason)"
            " VALUES ('design', 'Design', 'd', 'proposed', 'often')"
        )
        conn.execute(
            "INSERT INTO ideas (reel_id, category_slug, title, detail, why_relevant,"
            " confidence) VALUES ('a', 'design', 'title', 'd', 'w', 'medium')"
        )
    out = runner.invoke(app, ["category", "list"]).output
    assert out.splitlines()[0].split()[:3] == ["design", "proposed", "1"]

    r = runner.invoke(app, ["category", "reject", "design"])
    assert r.exit_code == 0 and "1 ideas now need a category" in r.output
    assert runner.invoke(app, ["category", "approve", "design"]).exit_code == 1
    assert "1 ideas need a category" in runner.invoke(app, ["status"]).output

    r = runner.invoke(app, ["recategorize"])
    assert "a  title" in r.output
    assert runner.invoke(app, ["recategorize", "1"]).exit_code == 1
    r = runner.invoke(app, ["recategorize", "1", "projects"])
    assert r.exit_code == 0 and "moved to projects" in r.output
    assert "No uncategorized ideas" in runner.invoke(app, ["recategorize"]).output


def test_category_add(settings):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["category", "add", "home-decor"])
    assert r.exit_code == 0 and "home-decor is active" in r.output
    assert "Home Decor" in runner.invoke(app, ["category", "list"]).output
    assert runner.invoke(app, ["category", "add", "home-decor"]).exit_code == 1
    assert runner.invoke(app, ["category", "add", "Bad Slug"]).exit_code == 1


def test_collections_lists_mapping(settings, monkeypatch):
    conn = _seed_reels(settings, [("a", "queued", 0)])
    with conn:
        conn.execute('UPDATE reels SET source_tags = \'["makan", "d1"]\'')
    monkeypatch.setattr(
        config,
        "settings",
        settings.model_copy(update={"collections": {"makan": "recipes", "d1": "nope"}}),
    )
    out = runner.invoke(app, ["collections"]).output
    assert "makan  ->  recipes\n" in out
    assert "d1  ->  nope (not an active category)" in out
    assert "0  (no collection)" in out


def test_emoji_output_survives_a_non_utf8_pipe(tmp_path):
    # Windows pipes default to cp1252: an agent reading `prism collections` got a crash
    env = {**os.environ, "PRISM_HOME": str(tmp_path), "PYTHONIOENCODING": "cp1252"}

    def prism(*args):
        code = "from prism.cli import app; app()"
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            env=env,
            capture_output=True,
            check=False,
        )

    assert prism("init").returncode == 0
    conn = db.connect(tmp_path / "prism.db")
    with conn:
        conn.execute(
            "INSERT INTO reels (id, source, source_id, url, status, source_tags)"
            " VALUES ('a', 'ig_export', 'a', 'u', 'queued', ?)",
            (json.dumps(["mamm🍴"]),),
        )
    r = prism("collections")
    assert r.returncode == 0, r.stderr
    assert "mamm🍴" in r.stdout.decode("utf-8")


def test_version_matches_the_package():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.output.strip() == f"prism {importlib.metadata.version('prism')}"
