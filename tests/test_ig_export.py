import json
import zipfile

import pytest

from prism.sources.ig_export import IgExportSource

# struktur meniru export asli 2026-11-02, isi karangan
EMOJI = "🍴".encode().decode("latin-1")  # cara export Meta menyimpan non-ASCII


def media(url, user="someone"):
    owner = {"dict": [{"label": "Username", "value": user}]}
    return [
        {"label": "URL", "value": url, "href": url},
        {"label": "Caption", "value": "c"},
        {"title": "Owner", "dict": [owner]},
    ]


POSTS = [
    {"timestamp": 1700000000, "fbid": "1", "media": [],
     "label_values": media("https://www.instagram.com/reel/AAA/", "al" + EMOJI)},
    {"timestamp": 1700000100, "fbid": "2", "media": [],
     "label_values": media("https://www.instagram.com/p/BBB/")},
]  # fmt: skip
COLLECTIONS = [
    {"timestamp": 1, "media": [], "label_values": [
        {"label": "Name", "value": "food" + EMOJI},
        {"title": "Media", "dict": [
            {"dict": media("https://www.instagram.com/reel/AAA/?igsh=x"), "title": ""},
            {"dict": media("https://www.instagram.com/reel/CCC/"), "title": ""},
            {"dict": [{"label": "Caption", "value": "post dihapus"}], "title": ""},
        ]},
    ]},
    {"timestamp": 2, "media": [], "label_values": [
        {"label": "Name", "value": "design"},
        {"title": "Media", "dict": [
            {"dict": media("https://www.instagram.com/reel/AAA/"), "title": ""},
        ]},
    ]},
]  # fmt: skip


def write_export(root):
    saved = root / "your_instagram_activity" / "saved"
    saved.mkdir(parents=True)
    (saved / "saved_posts.json").write_text(json.dumps(POSTS))
    (saved / "saved_collections.json").write_text(json.dumps(COLLECTIONS))
    return saved


def check(items):
    by = {i.source_id: i for i in items}
    assert sorted(by) == ["AAA", "BBB", "CCC"]  # CCC cuma ada di collections
    a = by["AAA"]
    assert (a.source, a.title, a.tags) == ("ig_export", "@al🍴", ["food🍴", "design"])
    assert a.saved_at == "2023-11-14T22:13:20+00:00"
    assert by["CCC"].saved_at is None and by["CCC"].tags == ["food🍴"]
    assert by["BBB"].tags == []


def test_folder_export(tmp_path):
    write_export(tmp_path / "export")
    check(IgExportSource(tmp_path / "export").list_new())


def test_zip_export(tmp_path):
    saved = write_export(tmp_path / "x")
    z = tmp_path / "instagram.zip"
    with zipfile.ZipFile(z, "w") as f:
        for p in saved.iterdir():
            f.write(p, p.relative_to(tmp_path / "x"))
    check(IgExportSource(z).list_new())


def test_not_an_export(tmp_path):
    with pytest.raises(FileNotFoundError):
        IgExportSource(tmp_path).list_new()
