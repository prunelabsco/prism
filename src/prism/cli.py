"""Prism CLI entry point. Command routing only."""

import io
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import typer

from prism import config, db

app = typer.Typer(
    help="Prism: turn saved Instagram reels into categorized ideas.",
    no_args_is_help=True,
)


@app.callback()
def _utf8_output() -> None:
    # Windows pipes default to cp1252, which can't print emoji in collection names
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def _log_to_file() -> None:
    """`prism` logger (yt-dlp included) goes to logs/prism.log, the terminal gets summaries."""
    logger = logging.getLogger("prism")
    if logger.handlers:
        return
    h = logging.FileHandler(config.settings.logs_dir / "prism.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _progress(message: str) -> None:
    # stderr: stdout keeps only the final summary
    typer.echo(message, err=True)


def _open() -> sqlite3.Connection:
    try:
        conn = db.open_existing(config.settings.db_path)
    except db.NotInitialized as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    config.settings.logs_dir.mkdir(parents=True, exist_ok=True)
    _log_to_file()
    return conn


@app.command()
def init() -> None:
    """Create the database, data folders and a config template. Safe to re-run."""
    s = config.settings
    existed = s.db_path.exists()
    for d in (s.reels_dir, s.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not config.CONFIG_FILE.exists():
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_FILE.write_text(config.TEMPLATE, encoding="utf-8")
        typer.echo(f"Config template: {config.CONFIG_FILE}")
    if not config.PROFILE_FILE.exists():
        config.PROFILE_FILE.write_text(config.PROFILE_TEMPLATE, encoding="utf-8")
        typer.echo(f"Profile template: {config.PROFILE_FILE}")
    conn = db.connect(s.db_path)
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    after = db.migrate(conn)
    if not existed:
        typer.echo(f"Database created: {s.db_path} (schema v{after})")
    elif after != before:
        typer.echo(f"Schema upgraded v{before} -> v{after}")
    else:
        typer.echo(f"Already initialized, schema v{after}")


@app.command()
def doctor() -> None:
    """Check ffmpeg, transcription, Instagram login and network, folders."""
    from prism import doctor as doctor_mod

    checks = doctor_mod.run()
    for c in checks:
        typer.echo(f"  {'ok ' if c.ok else 'FIX'}  {c.name:<16}{c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)
    typer.echo("All good.")


@app.command()
def fetch(
    file: Annotated[
        Path,
        typer.Argument(
            exists=True, help="Instagram export (ZIP or folder), or a URL list file"
        ),
    ],
    source: str = typer.Option("ig-export", help="ig-export | urls"),
    limit: int | None = typer.Option(None, help="Max new reels to queue"),
) -> None:
    """Queue new reels from an Instagram export or a URL list."""
    # imported here: yt-dlp is heavy, other commands don't need it
    from prism import fetch as fetch_mod
    from prism.sources.ig_export import IgExportSource
    from prism.sources.urls import UrlsSource

    if source not in ("urls", "ig-export"):
        raise typer.BadParameter("choose ig-export or urls", param_hint="--source")
    adapter = UrlsSource(file) if source == "urls" else IgExportSource(file)
    try:
        r = fetch_mod.run(_open(), adapter, limit=limit, progress=_progress)
    except (FileNotFoundError, ValueError) as e:  # broken / not an Instagram export
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None

    typer.echo(
        f"{r.new_reel + r.new_post + r.new_photo} new ({r.new_reel} reels,"
        f" {r.new_post} video posts, {r.new_photo} photo posts)"
        f" · {r.unreachable} unreachable"
        f" · {r.deferred} posts deferred"
        f" · {r.existing} already queued · {r.duplicates} duplicates"
        f" · {r.not_instagram} not Instagram"
    )
    if r.deferred:
        typer.echo("Deferred posts are checked on the next `prism fetch`.")
    if r.probe_stopped:
        typer.echo(f"Probing stopped (failed twice): {r.probe_stopped}", err=True)


@app.command()
def process(
    limit: int | None = typer.Option(
        None, help="Max reels to download this run (capped at max_per_run)"
    ),
) -> None:
    """Run the queue: download, extract frames, transcribe, build packets."""
    from prism import process as process_mod

    r = process_mod.run(_open(), limit=limit, progress=_progress)
    typer.echo(
        f"{r.downloaded} downloaded · {r.cached} cached"
        f" · {r.extracted} frame sets · {r.transcribed} transcribed"
        f" · {r.ready} ready to curate"
        f" · {r.transient} temporary errors (requeued) · {r.failed} failed"
        f" · {r.retried} auto-retried"
    )
    if r.stopped:
        typer.echo(f"Downloads stopped: {r.stopped}", err=True)


@app.command()
def retry(
    reel_id: str | None = typer.Argument(None, help="A `failed` reel to retry"),
    all_: bool = typer.Option(False, "--all", help="Retry every `failed` reel"),
    photos: bool = typer.Option(
        False, "--photos", help="Queue photo posts that were skipped (`not_video`)"
    ),
) -> None:
    """Send `failed` reels back to the queue (including ones that failed 3 times)."""
    if [reel_id is not None, all_, photos].count(True) != 1:
        typer.echo("Pass exactly one of: reel_id, --all, --photos.", err=True)
        raise typer.Exit(1)
    conn = _open()
    if photos:
        ids = [
            r["reel_id"]
            for r in conn.execute(
                "SELECT e.reel_id FROM events e JOIN reels r ON r.id = e.reel_id"
                " WHERE e.type = 'reel_skipped' AND r.status = 'skipped'"
                " AND json_extract(e.payload, '$.reason') = 'not_video'"
            )
        ]
        for i in ids:
            db.transition(conn, i, "queued")
        typer.echo(f"{len(ids)} photo posts queued. Run `prism process`.")
        return
    if all_:
        ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM reels WHERE status = 'failed'")
        ]
    else:
        row = conn.execute(
            "SELECT status FROM reels WHERE id = ?", (reel_id,)
        ).fetchone()
        if row is None or row["status"] != "failed":
            got = "does not exist" if row is None else f"is {row['status']}"
            typer.echo(
                f"Reel {reel_id} {got}, only `failed` reels can be retried.",
                err=True,
            )
            raise typer.Exit(1)
        ids = [reel_id]
    to = [db.retry(conn, i) for i in ids]
    typer.echo(
        f"{len(ids)} reels retried ({to.count('queued')} queued,"
        f" {to.count('ready')} ready to curate). Run `prism process`."
    )


@app.command("next")
def next_() -> None:
    """Take the next `ready` reel for curation, print its packet.json path."""
    from prism import curation

    path = curation.next_packet(_open())
    typer.echo(path if path else "No ready reels.")


@app.command()
def commit(
    reel_id: str = typer.Argument(..., help="The `curating` reel"),
    path: str | None = typer.Argument(None, help="curation.json"),
    fail: str | None = typer.Option(
        None, "--fail", help="Mark curation as failed with this reason"
    ),
) -> None:
    """Validate & store a curation result (or mark it failed with --fail)."""
    from prism import curation

    if (path is None) == (fail is None):
        typer.echo("Pass either a curation.json path or --fail.", err=True)
        raise typer.Exit(1)
    conn = _open()
    try:
        if fail is not None:
            curation.fail(conn, reel_id, fail)
            typer.echo(f"{reel_id} marked failed, auto-retry returns it to ready.")
            return
        r = curation.commit(conn, reel_id, Path(path))
    except (curation.CurationError, OSError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None

    if r.status == "duplicate":
        line = f"{reel_id} is a duplicate of {r.duplicate_of}"
    else:
        cats = f" ({', '.join(r.categories)})" if r.categories else ""
        line = f"{reel_id} curated: {r.idea_count} ideas{cats}"
    if r.proposed:
        line += f" + proposed category {r.proposed}"
    typer.echo(line)


category_app = typer.Typer(help="Manage categories (list, add, approve, reject).")
app.add_typer(category_app, name="category")


def _curation_call(fn, *args):
    """Call a curation function, turning CurationError into a message + exit 1."""
    from prism import curation

    try:
        return fn(*args)
    except curation.CurationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


@category_app.command("list")
def category_list() -> None:
    """Categories with status and idea count."""
    from prism import curation

    for c in curation.categories(_open()):
        line = f"  {c['slug']:<22}{c['status']:<10}{c['ideas']:>3} ideas  {c['name']}"
        if c["status"] == "proposed":
            line += f"  ({c['reason'] or '-'})"
        typer.echo(line)


@category_app.command("add")
def category_add(
    slug: str,
    name: str | None = typer.Argument(None, help="Display name (default: from slug)"),
    description: str | None = typer.Option(
        None, help="What belongs here, read by the agent"
    ),
) -> None:
    """Create your own category, active right away."""
    from prism import curation

    name = name or slug.replace("-", " ").title()
    _curation_call(curation.add, _open(), slug, name, description)
    typer.echo(f"Category {slug} is active.")


@category_app.command("approve")
def category_approve(slug: str) -> None:
    """Activate a proposed category."""
    from prism import curation

    _curation_call(curation.approve, _open(), slug)
    typer.echo(f"Category {slug} is active.")


@category_app.command("reject")
def category_reject(slug: str) -> None:
    """Reject a proposed category; its ideas become uncategorized."""
    from prism import curation

    n = _curation_call(curation.reject, _open(), slug)
    typer.echo(f"Category {slug} rejected, {n} ideas now need a category.")


@app.command()
def collections() -> None:
    """Instagram collections in the queue and the category each one maps to."""
    from prism import curation

    rows, loose = curation.collections(_open())
    for c in rows:
        target = c["slug"] or "-"
        if c["slug"] and not c["active"]:
            target += " (not an active category)"
        typer.echo(f"  {c['reels']:>4}  {c['name']}  ->  {target}")
    typer.echo(f"  {loose:>4}  (no collection)")
    typer.echo(f"Mappings live in [collections] in {config.CONFIG_FILE}")


@app.command()
def recategorize(
    idea_id: int | None = typer.Argument(None, help="An uncategorized idea"),
    slug: str | None = typer.Argument(None, help="Target core/approved category"),
) -> None:
    """No arguments: list uncategorized ideas. With arguments: move an idea."""
    from prism import curation

    conn = _open()
    if idea_id is None:
        rows = curation.uncategorized(conn)
        if not rows:
            typer.echo("No uncategorized ideas.")
        for r in rows:
            typer.echo(f"  {r['id']:>4}  {r['reel_id']}  {r['title']}")
        return
    if slug is None:
        typer.echo(
            "Name a target category: prism recategorize <idea_id> <slug>", err=True
        )
        raise typer.Exit(1)
    _curation_call(curation.recategorize, conn, idea_id, slug)
    typer.echo(f"Idea {idea_id} moved to {slug}.")


@app.command()
def export() -> None:
    """Write `curated` reels as Markdown notes (never overwrites notes you edited)."""
    from prism import export as export_mod

    r = export_mod.run(_open())
    typer.echo(
        f"{r.written} new notes · {r.rewritten} updated · {r.unchanged} unchanged"
        f" · {len(r.skipped)} skipped"
    )
    typer.echo(f"Notes: {config.settings.output_dir}")
    for reel_id, why in r.skipped:
        typer.echo(f"  skip {reel_id}: {why}")


@app.command()
def sync() -> None:
    """Read each note's status (new/picked/dropped) back into the database."""
    from prism import sync as sync_mod

    r = sync_mod.run(_open(), config.settings.vault_dir)
    typer.echo(
        f"{len(r.changed)} status changes · {len(r.moved)} notes moved"
        f" · {len(r.missing)} notes missing · {len(r.invalid)} invalid statuses"
    )
    for reel_id, a, b in r.changed:
        typer.echo(f"  {reel_id}: {a} -> {b}")
    for reel_id in r.missing:
        typer.echo(f"  not found under vault_dir: {reel_id}", err=True)
    for reel_id, st in r.invalid:
        typer.echo(f"  {reel_id}: status {st!r} is not new/picked/dropped", err=True)


@app.command()
def status(
    picked: bool = typer.Option(False, "--picked", help="Picked ideas by category"),
) -> None:
    """Counts per status & category."""
    if picked:
        rows = db.picked_ideas(_open())
        if not rows:
            typer.echo(
                "Nothing picked yet. Set a note's `status` to picked, then `prism sync`."
            )
        last = None
        for r in rows:
            if r["category"] != last:
                typer.echo(f"{r['category']}:")
                last = r["category"]
            typer.echo(f"  [{r['confidence']}] {r['title']}  ({r['reel_id']})")
        return
    s = db.status_summary(_open())

    if s["proposed"]:
        typer.echo("Proposed categories awaiting your decision:")
        for c in s["proposed"]:
            typer.echo(f"  {c['slug']} ({c['name']}): {c['reason'] or '-'}")
        typer.echo("")

    typer.echo("Reels:")
    if not s["counts"]:
        typer.echo("  none yet")
    for st in db.STATUS_ORDER:
        n = s["counts"].get(st, 0)
        if not n:
            continue
        line = f"  {st:<13}{n}"
        if st == "failed":
            auto = s["failed_auto_retry"]
            line += f"  ({auto} auto-retry, {n - auto} need `prism retry`)"
        typer.echo(line)

    typer.echo("\nActive categories:")
    for c in s["active"]:
        typer.echo(f"  {c['name']}: {c['ideas']} ideas")
    if s["uncategorized"]:
        typer.echo(
            f"  ({s['uncategorized']} ideas need a category, `prism recategorize`)"
        )
