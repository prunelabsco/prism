import tomllib

import pytest

from prism import config, doctor, login


def cookie(
    name="sessionid", value="SECRET", domain=".instagram.com", expires=4102444800
):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": True,
        "expires": expires,
    }


def test_written_file_passes_doctor_and_holds_instagram_only(tmp_path):
    f = tmp_path / "sub" / "c.txt"
    login.write_cookies(f, [cookie(), cookie("x", "OTHER", ".google.com")])
    assert doctor._cookies_file(f).ok
    text = f.read_text()
    assert "sessionid" in text and "OTHER" not in text
    assert f.stat().st_mode & 0o077 == 0


def test_session_cookie_expiry_is_written_as_zero(tmp_path):
    f = tmp_path / "c.txt"
    login.write_cookies(f, [cookie(expires=-1), cookie("csrftoken", expires=None)])
    assert doctor._cookies_file(f).ok
    assert all(line.split("\t")[4] == "0" for line in f.read_text().splitlines()[1:])


def test_logged_in_needs_an_instagram_session():
    assert login.logged_in([cookie()])
    assert not login.logged_in([cookie("csrftoken")])
    assert not login.logged_in([cookie(value="")])
    assert not login.logged_in([cookie(domain=".example.com")])


@pytest.mark.parametrize(
    "before",
    [
        "",
        '[paths]\noutput_dir = "x"\n',
        '[download]\nbrowser = "brave"\nmax_per_run = 5\n',
        '[download]\n# cookies_file = "~/old.txt"\nbrowser = "brave"\n',
        '[download]\ncookies_file = "~/old.txt"\n',
        '[download] # note\nbrowser = "brave"\n\n[collections]\n"a" = "b"\n',
    ],
)
def test_config_gets_cookies_file_and_keeps_the_rest(before, tmp_path):
    p = tmp_path / "with space" / "c.txt"
    out = tomllib.loads(login.with_cookies_file(before, p))
    assert out["download"]["cookies_file"] == p.as_posix()
    old = tomllib.loads(before)
    for section, values in old.items():
        for k, v in values.items():
            if k != "cookies_file":
                assert out[section][k] == v


def test_windows_path_is_valid_toml():
    p = login.Path("C:/Users/USER/.prism/c.txt")
    out = tomllib.loads(login.with_cookies_file("", p))
    assert out["download"]["cookies_file"] == "C:/Users/USER/.prism/c.txt"


def test_wait_returns_once_logged_in():
    answers = iter([[], [cookie("csrftoken")], [cookie()]])
    got = login.wait_for_session(lambda: next(answers), timeout=5, poll=0)
    assert login.logged_in(got)


def test_wait_times_out():
    with pytest.raises(login.LoginError, match="no login after"):
        login.wait_for_session(list, timeout=0, poll=0)


def test_wait_reports_a_closed_window():
    def closed():
        raise login._WindowClosed

    with pytest.raises(login.LoginError, match="closed"):
        login.wait_for_session(closed, timeout=5, poll=0)


def test_every_browser_is_known_on_each_system():
    for name, paths in login._PATHS.items():
        if name != "safari":
            assert set(paths) == {"darwin", "windows", "linux"}, name


def test_linux_finds_browsers_on_path(monkeypatch):
    monkeypatch.setattr(login.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert login.find_browser("brave", "linux") == login.Path("/usr/bin/brave-browser")


def test_pick_browser(monkeypatch, tmp_path):
    s = config.Settings(download=config.Download(browser="edge"))
    monkeypatch.setattr(config, "settings", s)
    exe = tmp_path / "arc"
    exe.touch()
    assert login.pick_browser(None, exe) == ("chromium", exe)

    monkeypatch.setattr(login, "find_browser", lambda n: exe if n == "edge" else None)
    assert login.pick_browser(None, None) == ("edge", exe)
    with pytest.raises(login.LoginError, match="not found"):
        login.pick_browser("brave", None)
    with pytest.raises(login.LoginError, match="don't know"):
        login.pick_browser("netscape", None)
    with pytest.raises(login.LoginError, match="not found"):
        login.pick_browser(None, tmp_path / "missing")
