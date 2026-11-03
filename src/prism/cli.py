"""Entry point CLI Prism. Hanya routing perintah."""

import logging
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from prism import config, db

app = typer.Typer(
    help="Prism: kurasi reels jadi ide berkategori.", no_args_is_help=True
)


def _log_to_file() -> None:
    """Logger `prism` (termasuk yt-dlp) ke data/logs/prism.log, terminal cukup ringkasan."""
    logger = logging.getLogger("prism")
    if logger.handlers:
        return
    h = logging.FileHandler(config.settings.logs_dir / "prism.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


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
    """Buat database & folder data. Aman dijalankan berulang."""
    s = config.settings
    existed = s.db_path.exists()
    for d in (s.reels_dir, s.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    conn = db.connect(s.db_path)
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    after = db.migrate(conn)
    if not existed:
        typer.echo(f"Database dibuat: {s.db_path} (skema v{after})")
    elif after != before:
        typer.echo(f"Skema diupgrade v{before} -> v{after}")
    else:
        typer.echo(f"Sudah di-init, skema v{after}")


@app.command()
def fetch(
    limit: int | None = typer.Option(None, help="Batas reel baru yang di-insert"),
    source: str = typer.Option("ig-export", help="ig-export | urls"),
    # Annotated: Path bukan tipe immutable, default typer.Option(...) kena ruff B008
    file: Annotated[
        Path | None,
        typer.Option(exists=True, help="Daftar URL, atau ZIP/folder export Instagram"),
    ] = None,
) -> None:
    """Tarik item baru dari sumber ke antrian."""
    # import di sini: yt-dlp berat, perintah lain tidak butuh
    from prism import fetch as fetch_mod

    if source in ("urls", "ig-export"):
        if file is None:
            raise typer.BadParameter(
                f"--source {source} butuh --file", param_hint="--file"
            )
        from prism.sources.ig_export import IgExportSource
        from prism.sources.urls import UrlsSource

        adapter = UrlsSource(file) if source == "urls" else IgExportSource(file)
        try:
            r = fetch_mod.run(_open(), adapter, limit=limit)
        except (FileNotFoundError, ValueError) as e:  # export rusak/bukan export IG
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    else:
        raise typer.BadParameter("pilih urls atau ig-export", param_hint="--source")

    typer.echo(
        f"{r.new_reel + r.new_post + r.new_photo} baru ({r.new_reel} reel,"
        f" {r.new_post} post video, {r.new_photo} post foto)"
        f" · {r.unreachable} post tidak bisa diakses"
        f" · {r.deferred} post ditunda"
        f" · {r.existing} sudah ada · {r.dup_cards} kartu duplikat"
        f" · {r.not_instagram} bukan Instagram"
    )
    if r.deferred:
        typer.echo("Post ditunda di-probe lagi pada `prism fetch` berikutnya.")
    if r.probe_stopped:
        typer.echo(f"Probe dihentikan (gagal 2x): {r.probe_stopped}", err=True)


@app.command()
def process(
    limit: int | None = typer.Option(
        None, help="Batas reel yang di-download run ini (maks max_per_run)"
    ),
) -> None:
    """Jalankan pipeline untuk reel di antrian (download, frame, transkrip, paket)."""
    from prism import process as process_mod

    r = process_mod.run(_open(), limit=limit)
    typer.echo(
        f"{r.downloaded} ter-download · {r.cached} sudah ada"
        f" · {r.extracted} frame diekstrak · {r.transcribed} ditranskrip"
        f" · {r.ready} siap dikurasi"
        f" · {r.rate_limited} kena rate limit (balik ke antrian) · {r.failed} gagal"
        f" · {r.retried} retry otomatis"
    )
    if r.stopped:
        typer.echo(f"Download dihentikan (rate limit/login 2x): {r.stopped}", err=True)


@app.command()
def retry(
    reel_id: str | None = typer.Argument(None, help="Reel `failed` yang diulang"),
    all_: bool = typer.Option(False, "--all", help="Ulang semua reel `failed`"),
    photos: bool = typer.Option(
        False, "--photos", help="Post foto yang dulu di-skip (`bukan_video`) ke antrian"
    ),
) -> None:
    """Kembalikan reel `failed` ke antrian (termasuk yang sudah 3x gagal)."""
    if [reel_id is not None, all_, photos].count(True) != 1:
        typer.echo("Sebut reel_id, --all, atau --photos (salah satu).", err=True)
        raise typer.Exit(1)
    conn = _open()
    if photos:
        ids = [
            r["reel_id"]
            for r in conn.execute(
                "SELECT e.reel_id FROM events e JOIN reels r ON r.id = e.reel_id"
                " WHERE e.type = 'reel_skipped' AND r.status = 'skipped'"
                " AND json_extract(e.payload, '$.reason') = 'bukan_video'"
            )
        ]
        for i in ids:
            db.transition(conn, i, "queued")
        typer.echo(f"{len(ids)} post foto ke antrian. Jalankan `prism process`.")
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
            got = "tidak ada" if row is None else f"status {row['status']}"
            typer.echo(
                f"Reel {reel_id} {got}, cuma reel `failed` yang bisa di-retry.",
                err=True,
            )
            raise typer.Exit(1)
        ids = [reel_id]
    to = [db.retry(conn, i) for i in ids]
    typer.echo(
        f"{len(ids)} reel di-retry ({to.count('queued')} ke antrian,"
        f" {to.count('ready')} ke siap dikurasi). Jalankan `prism process`."
    )


@app.command("next")
def next_() -> None:
    """Ambil reel `ready` berikutnya untuk dikurasi, cetak path packet.json."""
    from prism import curation

    path = curation.next_packet(_open())
    typer.echo(path if path else "Tidak ada reel ready.")


@app.command()
def commit(
    reel_id: str = typer.Argument(..., help="Reel `curating` yang dikurasi"),
    path: str | None = typer.Argument(None, help="curation.json"),
    fail: str | None = typer.Option(
        None, "--fail", help="Tandai kurasi gagal dengan alasan ini"
    ),
) -> None:
    """Validasi & simpan hasil kurasi (atau tandai gagal dengan --fail)."""
    from prism import curation

    if (path is None) == (fail is None):
        typer.echo("Sebut path curation.json atau --fail (salah satu).", err=True)
        raise typer.Exit(1)
    conn = _open()
    try:
        if fail is not None:
            curation.fail(conn, reel_id, fail)
            typer.echo(f"{reel_id} ditandai gagal, retry otomatis balik ke ready.")
            return
        r = curation.commit(conn, reel_id, Path(path))
    except (curation.CurationError, OSError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None

    if r.status == "duplicate":
        line = f"{reel_id} duplikat dari {r.duplicate_of}"
    else:
        cats = f" ({', '.join(r.categories)})" if r.categories else ""
        line = f"{reel_id} dikurasi: {r.idea_count} ide{cats}"
    if r.proposed:
        line += f" + usulan kategori {r.proposed}"
    typer.echo(line)


category_app = typer.Typer(help="Kelola kategori (usulan, approve, reject).")
app.add_typer(category_app, name="category")


def _curation_call(fn, *args):
    """Panggil fungsi curation, CurationError jadi pesan + exit 1."""
    from prism import curation

    try:
        return fn(*args)
    except curation.CurationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


@category_app.command("list")
def category_list() -> None:
    """Daftar kategori + status + jumlah ide."""
    from prism import curation

    for c in curation.categories(_open()):
        line = f"  {c['slug']:<22}{c['status']:<10}{c['ideas']:>3} ide  {c['name']}"
        if c["status"] == "proposed":
            line += f"  ({c['reason'] or '-'})"
        typer.echo(line)


@category_app.command("approve")
def category_approve(slug: str) -> None:
    """Aktifkan kategori usulan."""
    from prism import curation

    _curation_call(curation.approve, _open(), slug)
    typer.echo(f"Kategori {slug} aktif.")


@category_app.command("reject")
def category_reject(slug: str) -> None:
    """Tolak kategori usulan, ide di dalamnya jadi perlu kategori."""
    from prism import curation

    n = _curation_call(curation.reject, _open(), slug)
    typer.echo(f"Kategori {slug} ditolak, {n} ide jadi perlu kategori.")


@app.command()
def recategorize(
    idea_id: int | None = typer.Argument(None, help="Ide perlu kategori"),
    slug: str | None = typer.Argument(None, help="Kategori core/approved tujuan"),
) -> None:
    """Tanpa argumen: daftar ide perlu kategori. Dengan argumen: pindahkan ide."""
    from prism import curation

    conn = _open()
    if idea_id is None:
        rows = curation.uncategorized(conn)
        if not rows:
            typer.echo("Tidak ada ide perlu kategori.")
        for r in rows:
            typer.echo(f"  {r['id']:>4}  {r['reel_id']}  {r['title']}")
        return
    if slug is None:
        typer.echo(
            "Sebut kategori tujuan: prism recategorize <idea_id> <slug>", err=True
        )
        raise typer.Exit(1)
    _curation_call(curation.recategorize, conn, idea_id, slug)
    typer.echo(f"Ide {idea_id} pindah ke {slug}.")


@app.command()
def export() -> None:
    """Tulis reel `curated` ke Nexus (tidak menimpa catatan yang sudah diedit)."""
    from prism import export as export_mod

    r = export_mod.run(_open())
    typer.echo(
        f"{r.written} catatan baru · {r.rewritten} diperbarui · {r.unchanged} tidak berubah"
        f" · {len(r.skipped)} dilewati"
    )
    for reel_id, why in r.skipped:
        typer.echo(f"  lewati {reel_id}: {why}")


@app.command()
def sync() -> None:
    """Baca status catatan Nexus (baru/dipilih/dibuang) kembali ke database."""
    from prism import sync as sync_mod

    r = sync_mod.run(_open(), config.settings.nexus_vault)
    typer.echo(
        f"{len(r.changed)} status berubah · {len(r.moved)} catatan dipindah"
        f" · {len(r.missing)} catatan tidak ketemu · {len(r.invalid)} status tidak valid"
    )
    for reel_id, a, b in r.changed:
        typer.echo(f"  {reel_id}: {a} -> {b}")
    for reel_id in r.missing:
        typer.echo(f"  tidak ketemu di vault: {reel_id}", err=True)
    for reel_id, st in r.invalid:
        typer.echo(f"  {reel_id}: status {st!r} bukan baru/dipilih/dibuang", err=True)


@app.command()
def status(
    picked: bool = typer.Option(
        False, "--picked", help="Daftar ide terpilih per kategori"
    ),
) -> None:
    """Ringkasan jumlah per status & kategori."""
    if picked:
        rows = db.picked_ideas(_open())
        if not rows:
            typer.echo(
                "Belum ada ide dipilih. Ubah `status` catatan jadi dipilih, lalu `prism sync`."
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
        typer.echo("Usulan kategori menunggu keputusan:")
        for c in s["proposed"]:
            typer.echo(f"  {c['slug']} ({c['name']}): {c['reason'] or '-'}")
        typer.echo("")

    typer.echo("Reel:")
    if not s["counts"]:
        typer.echo("  belum ada")
    for st in db.STATUS_ORDER:
        n = s["counts"].get(st, 0)
        if not n:
            continue
        line = f"  {st:<13}{n}"
        if st == "failed":
            auto = s["failed_auto_retry"]
            line += f"  ({auto} retry otomatis, {n - auto} perlu `prism retry`)"
        typer.echo(line)

    typer.echo("\nKategori aktif:")
    for c in s["active"]:
        typer.echo(f"  {c['name']}: {c['ideas']} ide")
    if s["uncategorized"]:
        typer.echo(f"  ({s['uncategorized']} ide perlu kategori, `prism recategorize`)")
