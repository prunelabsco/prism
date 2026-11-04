import urllib.error
import urllib.request

from prism import config, doctor


class Cookie:
    def __init__(self, name, domain):
        self.name, self.domain = name, domain


def with_browser(monkeypatch, tmp_path, browser):
    s = config.Settings(
        paths=config.Paths(data_dir=tmp_path / "data", output_dir=tmp_path / "notes"),
        download=config.Download(browser=browser),
    )
    monkeypatch.setattr(config, "settings", s)


def test_no_browser_tells_where_to_set_it(monkeypatch, tmp_path):
    with_browser(monkeypatch, tmp_path, None)
    c = doctor._cookies()
    assert not c.ok and "[download]" in c.detail


def test_session_cookie_required(monkeypatch, tmp_path):
    import yt_dlp.cookies

    with_browser(monkeypatch, tmp_path, "chrome")
    jar = [Cookie("csrftoken", ".instagram.com")]
    monkeypatch.setattr(
        yt_dlp.cookies, "extract_cookies_from_browser", lambda b, p: jar
    )
    assert not doctor._cookies().ok
    jar.append(Cookie("sessionid", ".instagram.com"))
    assert doctor._cookies().ok


def test_output_dir_and_db(monkeypatch, tmp_path):
    with_browser(monkeypatch, tmp_path, None)
    assert doctor._output_dir().ok  # missing but creatable
    assert not doctor._db().ok


def test_reachable(monkeypatch):
    def answer(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many", None, None)

    def silent(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    assert doctor._reachable().ok  # any HTTP answer counts
    monkeypatch.setattr(urllib.request, "urlopen", silent)
    c = doctor._reachable()
    assert not c.ok and "another network" in c.detail
