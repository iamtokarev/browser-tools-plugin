"""The browser lifecycle against a real Chrome: shutdown, a lost browser, and who holds a profile.

Every Chrome here runs on a profile under `tmp_path`. A test that kills or crashes a Chrome
signals only the pid that its own session reported, and `chrome_for` checks every process by the
exact `--user-data-dir=` argument of that profile.
"""

import asyncio
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import psutil
import pytest
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin import facade
from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.lifecycle import ChromeProcess, Pidfile, scan_for_main, user_data_argument
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import needs_chrome

pytestmark = needs_chrome

LOST = "The browser closed unexpectedly ({}). Open tabs and page state are gone; call the tool again"


def returned(text: str) -> Any:
    """The JSON that page code returned as a string: the first paragraph of the result."""
    return json.loads(text.split("\n\n")[0])


def chrome_for(profile: Path) -> list[psutil.Process]:
    """Every process with the exact profile argument: the main one and its helpers."""
    found = []
    for process in psutil.process_iter(["cmdline"]):
        try:
            if user_data_argument(profile) in (process.info["cmdline"] or []):
                found.append(process)
        except psutil.Error:
            pass
    return [process for process in found if process.status() != psutil.STATUS_ZOMBIE]


class Browser:
    def __init__(self, settings: Settings) -> None:
        self.session = BrowserSession(settings)
        self.client = Client(create_server(session=self.session))

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

    async def raw(self, name: str, **arguments: object) -> tuple[str, bool]:
        result = await self.client.call_tool(name, arguments)
        (part,) = result.content
        assert isinstance(part, TextContent)
        return part.text, bool(result.is_error)

    async def call(self, name: str, **arguments: object) -> str:
        text, is_error = await self.raw(name, **arguments)
        assert not is_error, text
        return text

    async def run(self, code: str) -> str:
        return await self.call("run", code=code)

    async def goto(self, url: str) -> str:
        return await self.run(f"await page.goto({url!r}); return 'at ' + page.url();")

    @property
    def chrome(self) -> ChromeProcess:
        chrome = self.session._chrome
        assert chrome is not None
        return chrome


@pytest.fixture(autouse=True)
async def no_chrome_left(settings: Settings) -> AsyncIterator[None]:
    yield
    for process in chrome_for(settings.profile_dir):  # a failed test must not leave a Chrome behind
        process.kill()


async def gone(profile: Path, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while chrome_for(profile):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


# --- Shutdown -----------------------------------------------------------------------------------


async def test_a_close_stops_chrome_with_sigterm_in_a_fraction_of_the_grace_time(settings: Settings, fixture_url: str):
    browser = Browser(settings)
    async with browser:
        await browser.goto(f"{fixture_url}/other.html")
        chrome = browser.chrome
        assert chrome.running
        assert [process.pid for process in chrome_for(settings.profile_dir) if process.pid == chrome.pid] == [
            chrome.pid
        ]
    started = time.monotonic()
    await browser.session.close()  # a second close does nothing
    assert time.monotonic() - started < 1
    assert not chrome.running
    assert chrome_for(settings.profile_dir) == []
    assert not (settings.profile_dir.with_name("default.pid")).exists()


async def test_the_time_to_close_is_short(settings: Settings, fixture_url: str):
    browser = Browser(settings)
    await browser.client.__aenter__()
    await browser.goto(f"{fixture_url}/other.html")
    started = time.monotonic()
    await browser.session.close()
    elapsed = time.monotonic() - started
    await browser.client.__aexit__(None, None, None)
    print(f"close took {elapsed:.3f} s")
    assert elapsed < 5


async def test_local_storage_and_a_cookie_survive_a_restart(settings: Settings, fixture_url: str):
    async with Browser(settings) as first:
        await first.goto(f"{fixture_url}/other.html")
        await first.run(
            "await page.evaluate(() => { localStorage.setItem('kept', 'yes'); "
            "document.cookie = 'persisted=1; max-age=86400; path=/'; "
            "document.cookie = 'sessiononly=1; path=/'; });"
        )
    async with Browser(settings) as second:
        await second.goto(f"{fixture_url}/other.html")
        state = await second.run(
            "return await page.evaluate(() => JSON.stringify({ls: localStorage.getItem('kept'), "
            "cookie: document.cookie}));"
        )
    values = returned(state)
    assert values["ls"] == "yes"
    assert "persisted=1" in values["cookie"]
    assert "sessiononly" not in values["cookie"]  # a session cookie ends with the session, by design


async def test_a_pidfile_names_the_chrome_while_it_runs(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        recorded = Pidfile(settings.profile_dir.with_name("default.pid")).read()
        assert recorded == browser.chrome
        data = json.loads(settings.profile_dir.with_name("default.pid").read_text())
        assert set(data) == {"pid", "create_time", "port", "profile"}
        assert isinstance(data["port"], int)


# --- A Chrome that an earlier server left ---------------------------------------------------------


async def test_the_next_launch_stops_a_leftover_chrome_that_holds_the_profile(
    settings: Settings, fixture_url: str, tmp_path: Path
):
    profile = settings.profile_dir
    profile.mkdir(parents=True)
    assert settings.chrome_path is not None
    leftover = subprocess.Popen(  # an own Chrome, started the way an earlier server would have done
        [
            settings.chrome_path,
            user_data_argument(profile),
            "--headless=new",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        found = None
        while found is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            pid = scan_for_main(profile)
            found = ChromeProcess.of(pid, profile) if pid else None
        assert found is not None and found.pid == leftover.pid
        Pidfile(profile.with_name("default.pid")).write(found, port=None)
        async with Browser(settings) as browser:
            text = await browser.goto(f"{fixture_url}/other.html")
            assert text.startswith("at ")
            assert leftover.poll() is not None or not found.running  # the old Chrome is gone
            assert browser.chrome.pid != leftover.pid
    finally:
        if leftover.poll() is None:
            leftover.kill()
        leftover.wait()


# --- A lost browser -------------------------------------------------------------------------------


async def test_a_crashed_chrome_is_reported_once_and_the_next_call_relaunches(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        first = browser.chrome
        os.kill(first.pid, signal.SIGKILL)  # the pid of our own session only
        text, is_error = await browser.raw("snapshot")
        assert is_error
        assert text.startswith(LOST.format("the browser process exited")), text
        assert text.endswith("then navigate back.")
        assert not browser.session.launched
        assert "at " + fixture_url in await browser.goto(f"{fixture_url}/other.html")  # relaunched, no replay
        assert browser.chrome.pid != first.pid
    assert chrome_for(settings.profile_dir) == []


async def test_a_run_that_meets_a_crash_gives_the_lost_browser_error(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        pid = browser.chrome.pid
        killer = asyncio.get_running_loop().call_later(0.5, os.kill, pid, signal.SIGKILL)
        text, is_error = await browser.raw("run", code="await new Promise(r => setTimeout(r, 20000)); return 1;")
        killer.cancel()
        assert is_error
        assert text.startswith("The browser closed unexpectedly ("), text
        assert "Open tabs and page state are gone" in text
        assert (await browser.goto(f"{fixture_url}/other.html")).startswith("at ")


async def test_closing_the_last_tab_is_reported_once_and_the_next_call_relaunches(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        old = browser.chrome
        await browser.run("await page.close(); return 'closed';")
        text, is_error = await browser.raw("snapshot")
        assert is_error
        assert text.startswith(LOST.format("the last tab was closed")), text
        await asyncio.sleep(0.2)
        assert not old.running  # the empty browser was stopped, not left behind
        assert (await browser.goto(f"{fixture_url}/other.html")).startswith("at ")
        assert browser.chrome.pid != old.pid


async def test_there_is_no_keeper_tab(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        connection = browser.session._connection
        assert connection is not None
        reply = await connection.send("Target.getTargets")
        pages = [target for target in reply["targetInfos"] if target["type"] == "page"]
        assert len(pages) == 1


async def test_one_lock_serialises_every_call(settings: Settings, fixture_url: str):
    async with Browser(settings) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        code = (
            "const start = Date.now(); await new Promise(r => setTimeout(r, 400)); "
            "return JSON.stringify([start, Date.now()]);"
        )
        results = await asyncio.gather(*(browser.run(code) for _ in range(3)))
    spans = sorted(returned(r) for r in results)
    for earlier, later in itertools.pairwise(spans):
        assert earlier[1] <= later[0] + 5  # a later call starts after the earlier one ended


# --- Profiles ---------------------------------------------------------------------------------------


async def test_a_profile_that_another_server_holds_gives_a_throwaway_profile_that_is_deleted(
    settings: Settings, fixture_url: str
):
    async with Browser(settings) as holder:
        text = await holder.goto(f"{fixture_url}/other.html")
        assert "throwaway profile" not in text
        async with Browser(settings) as second:
            first_result = await second.goto(f"{fixture_url}/other.html")
            assert first_result.count(facade.THROWAWAY_NOTICE) == 1
            assert "throwaway profile" in first_result
            temporary = second.session._throwaway
            assert temporary is not None and temporary.is_dir()
            assert settings.profile_dir not in temporary.parents and temporary != settings.profile_dir
            assert chrome_for(temporary) != []
            later = await second.goto(f"{fixture_url}/other.html")
            assert "throwaway profile" not in later  # said once
        assert not temporary.exists()
        assert chrome_for(temporary) == []
        assert holder.chrome.running  # the holder was not touched
        assert (await holder.goto(f"{fixture_url}/other.html")).startswith("at ")


async def test_the_throwaway_profile_setting_forces_one_and_the_dedicated_profile_stays_unused(
    settings: Settings, fixture_url: str
):
    forced = replace(settings, throwaway_profile=True)
    async with Browser(forced) as browser:
        text = await browser.goto(f"{fixture_url}/other.html")
        assert "throwaway profile" not in text  # the user asked for it, so no notice
        temporary = browser.session._throwaway
        assert temporary is not None and temporary.is_dir()
    assert not temporary.exists()
    assert not settings.profile_dir.exists()


async def test_a_download_from_a_throwaway_profile_lands_in_the_download_directory(
    settings: Settings, fixture_url: str
):
    forced = replace(settings, throwaway_profile=True)
    async with Browser(forced) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        reported = await browser.run(
            """await page.evaluate(() => {
              document.body.innerHTML = '<a id="dl" href="/api/download?name=from-throwaway.txt">get</a>';
            });
            await page.locator('#dl').click();
            return 'clicked';"""
        )
        for _ in range(60):
            if "Downloaded:" in reported:
                break
            await asyncio.sleep(0.25)
            reported += await browser.call("snapshot")
    assert f"Downloaded: {settings.download_dir / 'from-throwaway.txt'}" in reported
    assert (settings.download_dir / "from-throwaway.txt").read_text() == "content of from-throwaway.txt"


async def test_the_profile_setting_names_the_dedicated_profile(settings: Settings, fixture_url: str):
    named = replace(settings, profile="work")
    async with Browser(named) as browser:
        await browser.goto(f"{fixture_url}/other.html")
        assert chrome_for(settings.data_dir / "profiles" / "work") != []
        assert chrome_for(settings.data_dir / "profiles" / "default") == []
    assert (settings.data_dir / "profiles" / "work").is_dir()
    assert not (settings.data_dir / "profiles" / "default").exists()


async def test_two_profile_names_run_side_by_side(settings: Settings, fixture_url: str):
    async with Browser(replace(settings, profile="a")) as one, Browser(replace(settings, profile="b")) as two:
        await one.goto(f"{fixture_url}/other.html")
        text = await two.goto(f"{fixture_url}/other.html")
        assert "throwaway profile" not in text


async def test_headed_shows_the_window_and_the_default_does_not(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    seen: list[dict[str, Any]] = []

    async def fake_launch(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(facade.local_browser, "launch", fake_launch)
    for headed in (False, True):
        session = BrowserSession(replace(settings, headed=headed))
        with pytest.raises(RuntimeError, match="stop here"):
            await session.run(code="return 1;")
        await session.close()
    assert [kwargs["headless"] for kwargs in seen] == [True, False]


# --- Signals over the real command -----------------------------------------------------------------


def server_process(data_dir: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "from browser_tools_plugin import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PLUGIN_DATA": str(data_dir)},
    )


def send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()


def read_until(process: subprocess.Popen[bytes], message_id: int) -> dict[str, Any]:
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        assert line, "the server closed its output"
        message = json.loads(line)
        if message.get("id") == message_id:
            return message


def launched_server(data_dir: Path) -> subprocess.Popen[bytes]:
    process = server_process(data_dir)
    send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )
    read_until(process, 1)
    send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "snapshot", "arguments": {}}})
    assert "error" not in read_until(process, 2)
    return process


@pytest.mark.parametrize("how", ["sigterm", "stdin_eof"])
def test_the_real_server_stops_chrome_on_sigterm_and_on_stdin_end(tmp_path: Path, how: str):
    data_dir = tmp_path / "data"
    profile = data_dir / "profiles" / "default"
    process = launched_server(data_dir)
    try:
        chrome_pid = scan_for_main(profile)
        assert chrome_pid is not None
        started = time.monotonic()
        if how == "sigterm":
            process.send_signal(signal.SIGTERM)
        else:
            assert process.stdin is not None
            process.stdin.close()
        process.wait(timeout=30)
        elapsed = time.monotonic() - started
        print(f"{how}: server exited after {elapsed:.3f} s")
        assert not psutil.pid_exists(chrome_pid) or psutil.Process(chrome_pid).status() == psutil.STATUS_ZOMBIE
        assert chrome_for(profile) == []
        assert not profile.with_name("default.pid").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream and not stream.closed:
                stream.close()
        shutil.rmtree(tmp_path / "unused", ignore_errors=True)
