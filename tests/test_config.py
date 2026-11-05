from prism import config


def test_defaults_without_file(tmp_path):
    s = config.load(tmp_path / "missing.toml")
    assert s.download.browser is None
    assert s.vault_dir == s.output_dir


def test_partial_override(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[paths]\noutput_dir = "/x/notes"\n[download]\nbrowser = "brave"\n')
    s = config.load(f)
    assert (s.output_dir.as_posix(), s.download.browser) == ("/x/notes", "brave")
    assert s.download.max_per_run == 30  # untouched keys keep defaults


def test_template_is_valid_toml(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(config.TEMPLATE)
    assert config.load(f) == config.Settings()


def test_collections_mapping(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[collections]\n"mamm🍴" = "recipes"\n', encoding="utf-8")
    assert config.load(p).collections == {"mamm🍴": "recipes"}
