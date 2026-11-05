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


def test_cookie_errors_say_what_to_do(monkeypatch, tmp_path):
    import yt_dlp.cookies

    with_browser(monkeypatch, tmp_path, "brave")

    def fails(msg):
        def extract(b, p):
            raise RuntimeError(msg)

        monkeypatch.setattr(yt_dlp.cookies, "extract_cookies_from_browser", extract)
        return doctor._cookies()

    assert "Close the browser" in fails("Could not copy Chrome cookie database.").detail
    assert "another browser" in fails("Failed to decrypt with DPAPI.").detail


def with_cookies_file(monkeypatch, tmp_path, content):
    f = tmp_path / "cookies.txt"
    if content is not None:
        f.write_text(content, encoding="utf-8")
    s = config.Settings(
        paths=config.Paths(data_dir=tmp_path / "data", output_dir=tmp_path / "notes"),
        download=config.Download(browser="brave", cookies_file=f),
    )
    monkeypatch.setattr(config, "settings", s)


def cookie_line(name, expires):
    return f".instagram.com\tTRUE\t/\tTRUE\t{expires}\t{name}\tSECRET\n"


NETSCAPE = "# Netscape HTTP Cookie File\n"
LATER = 4102444800  # 2100
PAST = 946684800  # 2000


def test_cookies_file_wins_over_browser(monkeypatch, tmp_path):
    import yt_dlp.cookies

    def never(*a):
        raise AssertionError("browser must not be read")

    monkeypatch.setattr(yt_dlp.cookies, "extract_cookies_from_browser", never)
    with_cookies_file(monkeypatch, tmp_path, NETSCAPE + cookie_line("sessionid", LATER))
    c = doctor._cookies()
    assert c.ok and "SECRET" not in c.detail


def test_cookies_file_problems_are_fix_lines(monkeypatch, tmp_path):
    cases = {
        None: "not found",
        "not a cookie file": "not a cookies.txt",
        NETSCAPE: "no live Instagram session",
        NETSCAPE + cookie_line("csrftoken", LATER): "no live Instagram session",
        NETSCAPE + cookie_line("sessionid", PAST): "no live Instagram session",
    }
    for content, expected in cases.items():
        with_cookies_file(monkeypatch, tmp_path, content)
        c = doctor._cookies()
        assert not c.ok and expected in c.detail and "SECRET" not in c.detail
        (tmp_path / "cookies.txt").unlink(missing_ok=True)


def test_cookie_values_never_reach_the_terminal(monkeypatch, tmp_path, capfd):
    bad_line = ".instagram.com\tTRUE\t/\tTRUE\tnot-a-date\tsessionid\tSECRET\n"
    with_cookies_file(monkeypatch, tmp_path, NETSCAPE + bad_line)
    c = doctor._cookies()
    out, err = capfd.readouterr()
    assert not c.ok and "SECRET" not in c.detail + out + err
