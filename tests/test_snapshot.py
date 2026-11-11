"""snapshot against a real headless Chrome and a local fixture page, in memory."""

import re
from collections.abc import AsyncIterator

import pytest
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import find_chrome, needs_chrome

pytestmark = needs_chrome


async def goto(session: BrowserSession, url: str) -> None:
    """Navigate the active page. The run tool is a later ticket, so reach in directly."""
    assert session._tools is not None
    await session._tools.run(code=f"await page.goto({url!r}); return 1;")


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(settings)
    try:
        yield session
    finally:
        await session.close()


async def snapshot_text(client: Client, **arguments: object) -> str:
    result = await client.call_tool("snapshot", arguments)
    assert not result.is_error, result.content
    (part,) = result.content
    assert isinstance(part, TextContent)
    return part.text


async def test_snapshot_returns_header_and_tree_with_element_ids(
    session: BrowserSession, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        assert not session.launched  # connecting and listing did not launch Chrome
        await snapshot_text(client)  # first call launches the browser, on about:blank
        assert session.launched
        await goto(session, f"{fixture_url}/page.html")
        text = await snapshot_text(client)

    header, _, tree = text.partition("\n")
    assert header == f"Page: Fixture page | {fixture_url}/page.html"
    assert re.search(r"\[\d+-\d+\] button: Greet", tree)
    assert re.search(r"\[\d+-\d+\] textbox: Name", tree)
    assert "Fixture heading" in tree


async def test_include_iframes_argument_is_honoured(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await snapshot_text(client)
        await goto(session, f"{fixture_url}/page.html")
        with_frames = await snapshot_text(client, includeIframes=True)
        without_frames = await snapshot_text(client, includeIframes=False)
    assert "Inside frame" in with_frames
    assert "Inside frame" not in without_frames


async def test_blank_page_header_has_empty_title(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        text = await snapshot_text(client)
    assert text.splitlines()[0] == "Page:  | about:blank"


async def test_failed_launch_is_a_tool_error_and_the_next_call_tries_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    session = BrowserSession(Settings(data_dir=tmp_path, chrome_path="/nonexistent/chrome"))
    async with Client(create_server(session=session)) as client:
        for _ in range(2):
            result = await client.call_tool("snapshot", {})
            assert result.is_error
            assert not session.launched


async def test_capture_deadline_setting_replaces_the_middlewares_constant(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from stagehand_middleware.facade import tools as upstream

    monkeypatch.setattr(upstream, "PAGE_CAPTURE_DEADLINE_S", 120.0)  # restored after the test
    session = BrowserSession(Settings(data_dir=tmp_path, capture_timeout=7.0, chrome_path=find_chrome()))
    try:
        async with Client(create_server(session=session)) as client:
            await snapshot_text(client)
        assert upstream.PAGE_CAPTURE_DEADLINE_S == 7.0
    finally:
        await session.close()
