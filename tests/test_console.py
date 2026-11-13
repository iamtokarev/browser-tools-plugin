"""The console tool and the capture buffer behind it."""

import asyncio
import re
import time
from collections.abc import AsyncIterator

import pytest
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin.capture import PageBuffer
from browser_tools_plugin.console_text import render_arguments
from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import needs_chrome

# --- The buffer, without a browser ---------------------------------------------------------


def logged(buffer: PageBuffer, count: int, level: str = "info") -> None:
    for i in range(count):
        buffer.add(at=time.time(), level=level, text=f"line {i}")


def test_buffer_keeps_the_last_1000_entries_and_counts_the_dropped():
    buffer = PageBuffer("t")
    logged(buffer, 1500)
    report = buffer.read(level="debug", all=True)
    assert buffer.held == 1000
    assert report.dropped == 500
    assert report.matching == 1000
    assert report.shown[0].text == "line 1499"  # newest first
    assert len(report.shown) == 100


def test_buffer_marks_navigation_and_survives_it():
    buffer = PageBuffer("t")
    logged(buffer, 2)
    buffer.add(at=time.time(), level="info", text="navigated to http://x/", marker=True)
    logged(buffer, 1)
    texts = [e.text for e in buffer.read(level="info", all=True).shown]
    assert texts == ["line 0", "navigated to http://x/", "line 1", "line 0"]


def test_a_marker_does_not_count_against_the_limit():
    buffer = PageBuffer("t", limit=3)
    logged(buffer, 3)
    buffer.add(at=time.time(), level="info", text="navigated", marker=True)
    assert buffer.dropped == 0
    assert buffer.held == 3


def test_new_entries_are_returned_once_and_all_returns_everything():
    buffer = PageBuffer("t")
    logged(buffer, 3)
    assert len(buffer.read(level="info", all=False).shown) == 3
    assert buffer.read(level="info", all=False).shown == []
    logged(buffer, 1)
    assert [e.text for e in buffer.read(level="info", all=False).shown] == ["line 0"]
    assert len(buffer.read(level="info", all=True).shown) == 4


def test_a_second_call_returns_the_older_entries_that_did_not_fit():
    buffer = PageBuffer("t")
    logged(buffer, 130)
    first = buffer.read(level="info", all=False)
    assert (len(first.shown), first.matching) == (100, 130)
    second = buffer.read(level="info", all=False)
    assert second.shown[0].text == "line 29"
    assert len(second.shown) == 30


def test_level_is_a_minimum_and_a_hidden_entry_stays_new():
    buffer = PageBuffer("t")
    buffer.add(at=0, level="debug", text="d")
    buffer.add(at=0, level="warning", text="w")
    buffer.add(at=0, level="error", text="e")
    assert [e.text for e in buffer.read(level="warning", all=False).shown] == ["e", "w"]
    assert [e.text for e in buffer.read(level="debug", all=False).shown] == ["d"]


def test_console_arguments_are_rendered_on_one_line():
    args = [
        {"type": "string", "value": "user %s has %d items"},
        {"type": "string", "value": "ann"},
        {"type": "number", "value": 3, "description": "3"},
        {
            "type": "object",
            "description": "Object",
            "preview": {
                "type": "object",
                "description": "Object",
                "properties": [
                    {"name": "a", "type": "number", "value": "1"},
                    {"name": "s", "type": "string", "value": "x"},
                ],
            },
        },
        {"type": "undefined"},
    ]
    assert render_arguments(args) == 'user ann has 3 items {a: 1, s: "x"} undefined'


# --- The tool, against a real Chrome -------------------------------------------------------


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(settings)
    try:
        yield session
    finally:
        await session.close()


async def call(client: Client, name: str, **arguments: object) -> str:
    result = await client.call_tool(name, arguments)
    (part,) = result.content
    assert isinstance(part, TextContent)
    assert not result.is_error, part.text
    return part.text


async def goto(client: Client, url: str) -> None:
    await call(client, "run", code=f"await page.goto({url!r}); return 1;")


async def console_until(client: Client, needle: str, **arguments: object) -> str:
    """Console output that holds `needle`, polling because a page can log after it loads."""
    seen = ""
    for _ in range(50):
        seen += "\n" + await call(client, "console", **arguments)
        if needle in seen:
            return seen
        await asyncio.sleep(0.1)
    pytest.fail(f"{needle!r} never appeared in:\n{seen}")


def lines_of(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"\d\d:\d\d:\d\d\.\d{3} ", line)]


@needs_chrome
async def test_first_navigation_is_captured_with_levels_locations_and_a_header(
    session: BrowserSession, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/console.html")
        text = await console_until(client, "Uncaught Error: boom")
    header = text.splitlines()[1]
    assert header.startswith("Console of page: Console fixture | ")
    lines = lines_of(text)
    assert any(
        re.search(r" info hello {a: 1, b: \"x\"} \[1, 2, 3\] \(.*/console\.html:\d+:\d+\)$", line) for line in lines
    )
    assert any(re.search(r" warning careful now \(", line) for line in lines)
    assert any(re.search(r" error bad thing \(", line) for line in lines)
    assert any(re.search(r" error Uncaught Error: boom \(.*/console\.html:\d+:\d+\)$", line) for line in lines)
    assert any("Failed to load resource" in line and "/missing.json" in line for line in lines)
    assert any(" info in frame (" in line for line in lines)  # an iframe's message lands on its page
    assert not any("debug detail" in line for line in lines)  # default level is info
    assert any(line.endswith(f"-- navigated to {fixture_url}/console.html --") for line in lines)


@needs_chrome
async def test_entries_are_newest_first_and_a_call_returns_only_what_is_new(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/console.html")
        await console_until(client, "Uncaught Error: boom")
        again = await call(client, "console")
        assert "0 new entries" in again
        assert lines_of(again) == []
        await call(client, "run", code="await page.locator('#log').click(); return 1;")
        fresh = await console_until(client, "clicked")
        assert [re.sub(r"^\S+ ", "", line).split(" (")[0] for line in lines_of(fresh)] == ["info clicked"]
        everything = await call(client, "console", all=True)
    messages = [re.sub(r"^\S+ ", "", line) for line in lines_of(everything)]
    assert messages[0].startswith("info clicked")  # newest first
    assert messages[-1].startswith("-- navigated to")  # the oldest entry is the marker
    assert len(messages) >= 8


@needs_chrome
async def test_level_debug_adds_debug_entries_and_error_hides_the_rest(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/console.html")
        await console_until(client, "Uncaught Error: boom", all=True)
        debug = await call(client, "console", all=True, level="debug")
        errors = await call(client, "console", all=True, level="error")
    assert any(" debug debug detail (" in line for line in lines_of(debug))
    assert lines_of(errors)
    assert all(" error " in line for line in lines_of(errors))


@needs_chrome
async def test_a_target_blank_popup_keeps_its_first_events(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/console.html")
        await call(
            client, "run", code="await page.locator('#popup').click(); await page.waitForTimeout(500); return 1;"
        )
        text = await console_until(client, "in popup")
        assert "Console of page: Popup fixture | " in text  # the popup is the active page
        assert any(line.endswith(f"-- navigated to {fixture_url}/popup.html --") for line in lines_of(text))
        # The first page is reachable by a part of its title, and is listed as another page.
        assert "Other pages (name one in page): Console fixture | " in text
        first = await call(client, "console", page="console fixture", all=True)
        assert "Console of page: Console fixture | " in first
        assert "bad thing" in first
        missing = await client.call_tool("console", {"page": "no such page"})
        assert missing.is_error
        (part,) = missing.content
        assert isinstance(part, TextContent)
        assert "matches no open page" in part.text
        assert "Popup fixture" in part.text


@needs_chrome
async def test_a_cross_site_iframe_is_captured_on_its_page(session: BrowserSession, fixture_url: str):
    other_site = fixture_url.replace("127.0.0.1", "localhost")  # another site, so another process
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/other.html")
        code = (
            "await page.evaluate(async (src) => { const f = document.createElement('iframe'); f.src = src; "
            "document.body.append(f); await new Promise(r => f.onload = r); }, "
            f"{other_site + '/frame.html'!r}); return 1;"
        )
        await call(client, "run", code=code)
        text = await console_until(client, "in frame")
    assert any(" info in frame (" in line and "localhost" in line for line in lines_of(text))


@needs_chrome
async def test_a_busy_page_is_cut_to_100_lines_and_long_text_is_cut(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/flood.html")
        text = await console_until(client, "(+200 chars)", all=True)
        again = await call(client, "console", all=True)
    assert len(lines_of(again)) == 100
    assert re.search(r" info x{300}…\(\+200 chars\) \(", text)
    assert "100 of 1000 entries" in again  # the buffer holds the last 1000 of about 1500
    assert re.search(r"\d+ older entries were dropped from this page's buffer", again)


@needs_chrome
async def test_console_before_any_browser_does_not_launch_one(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        text = await call(client, "console")
    assert "No browser is open yet" in text
    assert not session.launched


@needs_chrome
async def test_without_the_debugging_group_nothing_is_captured(settings: Settings, fixture_url: str):
    session = BrowserSession(Settings(data_dir=settings.data_dir, chrome_path=settings.chrome_path, groups=("core",)))
    try:
        async with Client(create_server(session=session)) as client:
            assert "console" not in [t.name for t in (await client.list_tools()).tools]
            await goto(client, f"{fixture_url}/console.html")
            assert session.launched
            assert not session.capturing
            assert (await call(client, "snapshot")).startswith("Page: Console fixture")
    finally:
        await session.close()
