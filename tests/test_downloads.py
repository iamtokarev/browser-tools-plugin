"""Downloads, against a real Chrome. Every file must land in `download_dir`, under a safe name.

The "tmp tree" checks list the whole of `tmp_path`, so a file that went anywhere but
`download_dir` shows up as a stray. The working directory is checked too. `~/Downloads` is not
read by the tests: nothing in them can point Chrome there, and the run is checked from outside.
"""

import asyncio
import json
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import pytest
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin import downloads as downloads_module
from browser_tools_plugin.cdp import CdpConnection
from browser_tools_plugin.downloads import (
    Download,
    Downloads,
    downloads_notice,
    neutral_name,
    safe_name,
    seed_download_directory,
    unique_path,
)
from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import OtherSite, needs_chrome

# --- Names and preferences, without a browser ----------------------------------------------


@pytest.mark.parametrize(
    ("suggested", "expected"),
    [
        ("report.csv", "report.csv"),
        ("../../x", "x"),
        ("../../x.txt", "x.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\evil.exe", "evil.exe"),
        ("..\\..\\x.txt", "x.txt"),
        ("..", "download"),
        (".", "download"),
        ("", "download"),
        (".bashrc", "bashrc"),
        ("a/b/", "download"),
        ("tab\tand\nnewline.txt", "tab_and_newline.txt"),
        ("nul\x00byte.txt", "nul_byte.txt"),
        ('quo"te<>|?*.txt', "quo_te_.txt"),
        ("archive.tar.gz", "archive.tar.gz"),
    ],
)
def test_a_suggested_name_is_cut_to_one_safe_file_name(suggested: str, expected: str):
    assert safe_name(suggested) == expected


@pytest.mark.parametrize(
    "name",
    [
        "x.command", "x.app", "x.sh", "x.exe", "x.pkg", "x.dmg", "x.terminal", "x.scpt", "x.jar", "x.bat",
        "x.ps1", "x.desktop", "x.AppImage", "X.EXE", "x.Command", "archive.tar.sh", "x.exe.", "x.exe ", "x.lnk",
    ],
)  # fmt: skip
def test_a_launchable_extension_gets_a_neutral_suffix(name: str):
    neutral, changed = neutral_name(name)
    assert changed
    assert neutral == f"{name}.download"
    assert not neutral.lower().rstrip(". ").endswith(tuple(f".{e}" for e in ("exe", "sh", "command")))


@pytest.mark.parametrize(
    "name", ["report.csv", "photo.jpg", "notes.txt", "archive.tar.gz", "exe", "sh", "download", "a.pdf"]
)
def test_an_ordinary_name_is_left_alone(name: str):
    assert neutral_name(name) == (name, False)


def test_a_long_name_is_cut_and_keeps_its_extension():
    name = safe_name("é" * 300 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode()) <= 120


def test_a_taken_name_gets_a_number_and_a_dangling_link_counts_as_taken(tmp_path: Path):
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "a (1).txt").write_text("2")
    (tmp_path / "b").symlink_to(tmp_path / "missing")
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"
    assert unique_path(tmp_path, "b") == tmp_path / "b (1)"
    assert unique_path(tmp_path, "new.txt") == tmp_path / "new.txt"


def test_seeding_a_fresh_profile_writes_the_directory(tmp_path: Path):
    profile, target = tmp_path / "profile", tmp_path / "downloads"
    seed_download_directory(profile, target)
    preferences = json.loads((profile / "Default" / "Preferences").read_text())
    assert preferences["download"]["default_directory"] == str(target)
    assert preferences["download"]["prompt_for_download"] is False
    assert preferences["savefile"]["default_directory"] == str(target)


def test_seeding_replaces_an_older_directory_and_keeps_other_preferences(tmp_path: Path):
    profile, target = tmp_path / "profile", tmp_path / "downloads"
    path = profile / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"download": {"default_directory": "/elsewhere", "extensions_to_open": "x"}, "keep": {"me": 1}})
    )
    seed_download_directory(profile, target)
    preferences = json.loads(path.read_text())
    assert preferences["download"]["default_directory"] == str(target)
    assert preferences["download"]["extensions_to_open"] == "x"
    assert preferences["keep"] == {"me": 1}
    assert list(path.parent.iterdir()) == [path]  # no temporary file is left


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"download": 5, "savefile": "x"}', ""])
def test_seeding_survives_a_broken_preferences_file(tmp_path: Path, content: str):
    profile, target = tmp_path / "profile", tmp_path / "downloads"
    path = profile / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    seed_download_directory(profile, target)
    assert json.loads(path.read_text())["download"]["default_directory"] == str(target)


def test_the_notice_gives_the_path_and_says_the_file_was_not_opened(tmp_path: Path):
    notice = downloads_notice([Download("http://x.test/a", tmp_path / "a.txt", 12)])
    assert f"Downloaded: {tmp_path / 'a.txt'} (12 bytes) from http://x.test/a." in notice
    assert "does not open or run it" in notice
    named = downloads_notice([Download("http://x.test/a", tmp_path / "a.sh.download", 3, neutralised=True)])
    assert "added .download to the name" in named
    assert "did not finish" in downloads_notice([Download("http://x.test/a", None)])
    assert "still running" in downloads_notice([], 1)
    assert downloads_notice([], 0) == ""


# --- A real Chrome -------------------------------------------------------------------------


class Browser:
    def __init__(self, settings: Settings) -> None:
        self.session = BrowserSession(settings)
        self.client = Client(create_server(session=self.session))
        self.reported: list[str] = []  # every path that a result gave, in order

    async def __aenter__(self) -> "Browser":
        await self.client.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            await self.client.__aexit__(exc_type, exc, traceback)
        finally:
            await self.session.close()

    async def call(self, name: str, **arguments: object) -> str:
        result = await self.client.call_tool(name, arguments)
        (part,) = result.content
        assert isinstance(part, TextContent)
        assert not result.is_error, part.text
        self.reported += re.findall(r"Downloaded: (.+?) \(\d+ bytes\)", part.text)
        return part.text

    async def run(self, code: str) -> str:
        return await self.call("run", code=code)

    async def goto(self, url: str) -> str:
        return await self.run(f"await page.goto({url!r}); return 'at ' + page.url();")

    async def until_downloaded(self, count: int = 1) -> list[str]:
        """The paths that results have reported so far, after polling with cheap calls until `count` are there."""
        for _ in range(60):
            if len(self.reported) >= count:
                return self.reported
            await self.call("snapshot")
            await asyncio.sleep(0.25)
        raise AssertionError(f"only {self.reported} reported")


@pytest.fixture
def working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty working directory, so that a stray file written to it shows up."""
    directory = tmp_path / "cwd"
    directory.mkdir()
    monkeypatch.chdir(directory)
    yield directory
    assert list(directory.iterdir()) == []


def tree(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


def strays(tmp_path: Path, settings: Settings) -> set[Path]:
    """Files under tmp_path that are neither in the download directory nor part of the browser profile."""
    return {
        path
        for path in tree(tmp_path)
        if settings.download_dir not in path.parents and settings.profile_dir not in path.parents
    }


def saved(settings: Settings) -> dict[str, str]:
    return {path.name: path.read_text() for path in settings.download_dir.iterdir()}


async def click_link(browser: Browser, site: str, href: str, attributes: str = "") -> str:
    await browser.goto(f"{site}/other.html")
    return await browser.run(
        f"""await page.evaluate(() => {{
          document.body.innerHTML = '<a id="dl" href="{href}" {attributes}>get</a>';
        }});
        await page.locator('#dl').click();
        return 'clicked';"""
    )


@needs_chrome
async def test_a_link_click_saves_the_file_and_the_result_gives_the_path(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    async with Browser(settings) as browser:
        await click_link(browser, fixture_url, "/api/download?name=report.txt")
        paths = await browser.until_downloaded()
    target = settings.download_dir / "report.txt"
    assert saved(settings) == {"report.txt": "content of report.txt"}
    assert paths == [str(target)]
    assert not (stat.S_IMODE(target.stat().st_mode) & 0o111)  # never executable
    assert strays(tmp_path, settings) == set()


@needs_chrome
async def test_a_launchable_file_gets_a_neutral_suffix_and_keeps_the_quarantine_attribute(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    async with Browser(settings) as browser:
        await click_link(browser, fixture_url, "/api/download?name=Run%20Me.COMMAND")
        paths = await browser.until_downloaded()
    target = settings.download_dir / "Run Me.COMMAND.download"
    assert paths == [str(target)]
    assert saved(settings) == {"Run Me.COMMAND.download": "content of Run Me.COMMAND"}
    assert not (stat.S_IMODE(target.stat().st_mode) & 0o111)
    if sys.platform == "darwin":
        names = subprocess.run(["xattr", str(target)], capture_output=True, text=True).stdout.split()
        # Chrome sets it in allowAndName mode (measured), and the rename links the same file, so it stays.
        assert "com.apple.quarantine" in names


@needs_chrome
async def test_a_hostile_filename_cannot_leave_the_directory(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    hostile = [
        'attachment; filename="../../x.txt"',
        'attachment; filename="/tmp/browser-tools-absolute.txt"',
        "attachment; filename*=UTF-8''..%2F..%2Fencoded.txt",
        'attachment; filename="..\\..\\back.txt"',
    ]
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        for header in hostile:
            url = f"{fixture_url}/api/download?disposition={header.replace(' ', '%20').replace(chr(34), '%22')}"
            await browser.run(f"await page.goto({url!r}).catch(() => 0); return 1;")
        reported = await browser.until_downloaded(len(hostile))
    files = {path.name for path in settings.download_dir.iterdir()}
    assert len(files) == len(hostile), files
    for path in reported:
        assert Path(path).parent == settings.download_dir
    assert not Path("/tmp/browser-tools-absolute.txt").exists()
    assert strays(tmp_path, settings) == set()
    assert tmp_path / "x.txt" not in tree(tmp_path)


@needs_chrome
async def test_page_code_downloads_by_a_script_click_and_by_a_blob_land_in_the_directory(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    code = """await page.evaluate(() => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob(['made by page code']));
      a.download = '../../blob.txt';
      document.body.appendChild(a);
      a.click();
    });
    return 'done';"""
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        await browser.run(code)
        paths = await browser.until_downloaded()
    # Chrome itself turns `../../blob.txt` into `_.._blob.txt`; the server then keeps that name.
    assert saved(settings) == {"_.._blob.txt": "made by page code"}
    assert paths == [str(settings.download_dir / "_.._blob.txt")]
    assert strays(tmp_path, settings) == set()


@needs_chrome
async def test_a_navigation_to_an_attachment_saves_it_and_leaves_the_page(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        # `page.goto` itself would wait 15 s and fail, because the SDK sees no navigation start.
        await browser.run("await page.evaluate(() => { location.href = '/api/download?name=nav.txt'; }); return 1;")
        await browser.until_downloaded()
        snapshot = await browser.call("snapshot")
    assert saved(settings) == {"nav.txt": "content of nav.txt"}
    assert snapshot.startswith("Page: ")
    assert strays(tmp_path, settings) == set()


@needs_chrome
async def test_two_downloads_with_the_same_name_keep_both_files(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    async with Browser(settings) as browser:
        await click_link(browser, fixture_url, "/api/download?name=same.txt&body=first")
        await browser.until_downloaded()
        await click_link(browser, fixture_url, "/api/download?name=same.txt&body=second")
        await browser.until_downloaded()
        await click_link(browser, fixture_url, "/api/download?name=same.txt&body=third")
        await browser.until_downloaded()
    assert saved(settings) == {"same.txt": "first", "same (1).txt": "second", "same (2).txt": "third"}


@needs_chrome
async def test_a_download_from_a_refused_site_does_not_happen(
    settings: Settings, fixture_url: str, other_site: OtherSite, tmp_path: Path, working_directory: Path
):
    port = int(fixture_url.rsplit(":", 1)[1])
    limited = replace(settings, allow_sites=(f"a.localhost:{port}",))
    site_a = f"http://a.localhost:{port}"
    refused = other_site.url("b.localhost", "/attachment.txt")
    async with Browser(limited) as browser:
        await browser.goto(f"{site_a}/other.html")
        by_click = await browser.run(
            f"""await page.evaluate(() => {{
              document.body.innerHTML = '<a id="dl" href="{refused}" download>get</a>';
            }});
            await page.locator('#dl').click();
            return 'clicked';"""
        )
        by_page_code = await browser.run(f"await page.goto({refused!r}); return 'went';")
        await asyncio.sleep(1)
        later = await browser.call("snapshot")
        # The allowed site still downloads.
        await click_link(browser, site_a, "/api/download?name=allowed.txt")
        await browser.until_downloaded()
    assert saved(limited) == {"allowed.txt": "content of allowed.txt"}
    assert other_site.hits == []
    assert "Refused by site limits: " in by_click + by_page_code
    assert "Downloaded:" not in by_click + by_page_code + later.split("allowed")[0]


@needs_chrome
@pytest.mark.parametrize("groups", [("core",), ("core", "debugging")], ids=["debugging-off", "debugging-on"])
async def test_downloads_work_with_the_debugging_group_off(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path, groups: tuple[str, ...]
):
    configured = replace(settings, groups=groups)
    async with Browser(configured) as browser:
        await click_link(browser, fixture_url, "/api/download?name=groups.txt")
        paths = await browser.until_downloaded()
        assert (browser.session.capturing) == ("debugging" in groups)
    assert paths == [str(settings.download_dir / "groups.txt")]
    assert strays(tmp_path, settings) == set()


@needs_chrome
async def test_no_download_lands_outside_the_directory_when_the_host_connection_does_not_hold_the_setting(
    settings: Settings,
    fixture_url: str,
    tmp_path: Path,
    working_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The host connection sets nothing. Only the seeded preference is left, and it is enough.

    The profile starts with preferences that point somewhere else: the server's directory must win.
    """
    elsewhere = tmp_path / "elsewhere"
    old = settings.profile_dir / "Default" / "Preferences"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"download": {"default_directory": str(elsewhere)}}))

    async def hold_nothing(self: Downloads) -> None:
        return None

    monkeypatch.setattr(Downloads, "assert_directory", hold_nothing)
    async with Browser(settings) as browser:
        await click_link(browser, fixture_url, "/api/download?name=pref.txt&body=one")
        await asyncio.sleep(1.5)
        await click_link(browser, fixture_url, "/api/download?name=pref.txt&body=two")
        await asyncio.sleep(1.5)
    contents = sorted(path.read_text() for path in settings.download_dir.iterdir())
    assert contents == ["one", "two"]  # a name that is taken is numbered by Chrome itself
    assert not elsewhere.exists()
    assert strays(tmp_path, settings) == set()


@needs_chrome
async def test_page_code_that_points_the_directory_elsewhere_is_set_back_before_the_next_call(
    settings: Settings, fixture_url: str, tmp_path: Path, working_directory: Path
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        stagehand = browser.session._stagehand
        assert stagehand is not None
        url = stagehand.browser.cdp_url
        assert url is not None
        rival = await CdpConnection.connect(url)
        try:
            await rival.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(elsewhere)})
            # The next call holds the directory again, then the click starts the download.
            await click_link(browser, fixture_url, "/api/download?name=held.txt")
            await browser.until_downloaded()
        finally:
            await rival.close()
    assert saved(settings) == {"held.txt": "content of held.txt"}
    assert list(elsewhere.iterdir()) == []


async def test_a_file_that_is_not_in_the_directory_is_reported_and_left_alone(tmp_path: Path):
    """If Chrome saved a file somewhere else, the server moves nothing there: it only says where it is."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guid-1").write_text("data")
    directory = tmp_path / "downloads"
    directory.mkdir()
    handler = Downloads(CdpConnection(), directory)
    result = handler._name_file(
        "guid-1", downloads_module._Pending("http://x.test/a", "named.txt"), str(outside / "guid-1")
    )
    assert result == Download("http://x.test/a", outside / "guid-1", 4, outside=True)
    assert (outside / "guid-1").exists()
    assert list(directory.iterdir()) == []
    assert "left the file there" in downloads_notice([result])
