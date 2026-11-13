"""Site limits and the file block, against a real Chrome and two local origins.

Site A is the fixture server, reached as `a.localhost`. Site B is a second server on another
port that counts the requests it gets. `*.localhost` names are not exempt from an allow list, so
B is refused by name, and the hit counter shows that no request left the browser.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import pytest
import websockets
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import OtherSite, needs_chrome

pytestmark = needs_chrome


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


@pytest.fixture
def site_a(fixture_url: str) -> str:
    return f"http://a.localhost:{port_of(fixture_url)}"


@pytest.fixture
def site_b(other_site: OtherSite) -> str:
    return other_site.url("b.localhost", "")


@pytest.fixture
def allow_a(settings: Settings, fixture_url: str) -> Settings:
    return replace(settings, allow_sites=(f"a.localhost:{port_of(fixture_url)}",))


class Browser:
    """A started server with one session, for one test."""

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

    async def snapshot(self) -> str:
        return await self.call("snapshot")


@pytest.fixture
async def browser_factory() -> AsyncIterator[list[Browser]]:
    opened: list[Browser] = []
    yield opened
    for browser in opened:
        await browser.session.close()


async def settle(seconds: float = 0.6) -> None:
    await asyncio.sleep(seconds)


# --- Navigation, redirects and links -------------------------------------------------------


async def test_a_navigation_to_a_refused_site_shows_a_blocked_page_and_sends_nothing(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    async with Browser(allow_a) as browser:
        assert "at " + site_a in await browser.goto(f"{site_a}/other.html")
        result = await browser.goto(f"{site_b}/secret?token=FAKE123")
        snapshot = await browser.snapshot()
    assert f"Refused by site limits: {site_b}/secret (its site is not on the allow list)" in result
    assert "FAKE123" not in result.partition("Refused by site limits")[2]
    assert (
        "Blocked by site limits: " + site_b + "/secret was not opened, because its site is not on the allow list."
        in snapshot
    )
    assert snapshot.startswith("Page: Blocked by site limits | ")
    assert "Other site reached" not in snapshot
    await settle()
    assert other_site.hits == []


async def test_a_redirect_to_a_refused_site_is_refused_at_the_hop(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    async with Browser(allow_a) as browser:
        result = await browser.goto(f"{site_a}/api/redirect-to?url={site_b}/landing")
        snapshot = await browser.snapshot()
    assert "Refused by site limits: " + site_b + "/landing" in result
    assert "Blocked by site limits: " in snapshot
    await settle()
    assert other_site.hits == []


async def test_a_link_click_from_page_code_and_from_an_action_is_refused(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    link = f'document.body.innerHTML = \'<a id="l" href="{site_b}/clicked">go to b</a>\';'
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        await browser.run(f"await page.evaluate(() => {{ {link} }}); await page.locator('#l').click(); return 1;")
        first = await browser.snapshot()
        await browser.goto(f"{site_a}/other.html")
        await browser.run(f"await page.evaluate(() => {{ {link} }}); return 1;")
        tree = await browser.snapshot()
        element = re.search(r"\[(\d+-\d+)\] link", tree)
        assert element, tree
        acted = await browser.call("run", actions=[{"op": "click", "id": element.group(1)}])
        second = await browser.snapshot()
    assert "Blocked by site limits: " + site_b + "/clicked" in first
    assert "Blocked by site limits: " + site_b + "/clicked" in second
    assert "Refused by site limits: " + site_b + "/clicked" in acted
    await settle()
    assert other_site.hits == []


# --- Subresources, iframes and popups ------------------------------------------------------


async def test_xhr_fetch_images_and_workers_to_a_refused_site_never_leave(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    code = f"""
    return await page.evaluate(async () => {{
      const out = [];
      out.push(await fetch({site_b + "/f"!r}).then(() => 'sent', () => 'refused'));
      out.push(await new Promise(r => {{
        const x = new XMLHttpRequest(); x.open('GET', {site_b + "/x"!r});
        x.onload = () => r('sent'); x.onerror = () => r('refused'); x.send(); }}));
      out.push(await new Promise(r => {{
        const i = new Image(); i.onload = () => r('sent'); i.onerror = () => r('refused');
        i.src = {site_b + "/i.png"!r}; }}));
      const source = "fetch(" + JSON.stringify({site_b + "/w"!r}) + ")"
        + ".then(() => postMessage('sent'), () => postMessage('refused'))";
      out.push(await new Promise(r => {{
        const w = new Worker(URL.createObjectURL(new Blob([source]))); w.onmessage = e => r(e.data); }}));
      return out.join(',');
    }});
    """
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        result = await browser.run(code)
    assert result.startswith("refused,refused,refused,refused"), result
    assert "Refused by site limits: " in result
    assert other_site.hits == []


async def test_fetch_in_the_page_code_scope_and_not_in_the_page_is_refused_too(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    """Page code runs in the extension, where a bare `fetch` is not a request of the page."""
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        result = await browser.run(
            f"try {{ const r = await fetch({site_b + '/bare'!r}); return 'sent ' + r.status; }} "
            "catch (e) { return 'refused'; }"
        )
    await settle()
    assert result.startswith("refused"), result
    assert other_site.hits == []


async def test_an_iframe_on_a_refused_site_shows_the_blocked_page_in_its_place(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    code = (
        "await page.evaluate(() => { const f = document.createElement('iframe'); f.title = 'embedded';"
        f" f.src = {site_b + '/frame'!r}; document.body.append(f); }});"
        " await page.waitForTimeout(500); return 1;"
    )
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        result = await browser.run(code)
        snapshot = await browser.snapshot()
    assert "Refused by site limits: " + site_b + "/frame" in result
    assert "Blocked by site limits: " + site_b + "/frame" in snapshot
    assert other_site.hits == []


async def test_a_popup_and_a_blank_link_to_a_refused_site_are_refused(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    code = f"""
    await page.evaluate(() => {{
      const b = document.createElement('button'); b.id = 'b'; b.textContent = 'open';
      b.onclick = () => window.open({site_b + "/popup"!r});
      const a = document.createElement('a'); a.id = 'l'; a.textContent = 'x';
      a.href = {site_b + "/blank"!r}; a.target = '_blank';
      document.body.append(b, a);
    }});
    await page.locator('#b').click(); await page.waitForTimeout(500);
    await page.locator('#l').click(); await page.waitForTimeout(700);
    return 1;
    """
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        result = await browser.run(code)
        snapshot = await browser.snapshot()
    assert "Refused by site limits: " in result
    assert site_b + "/popup" in result and site_b + "/blank" in result
    assert "Blocked by site limits: " in snapshot  # a popup is the active page and shows why
    await settle()
    assert other_site.hits == []


# --- The rules -----------------------------------------------------------------------------


async def test_deny_beats_allow_and_a_deny_list_alone_refuses_only_what_it_names(
    settings: Settings, fixture_url: str, site_a: str, other_site: OtherSite
):
    denied = replace(settings, deny_sites=(f"127.0.0.1:{other_site.port}",))
    async with Browser(denied) as browser:
        assert "at " + fixture_url in await browser.goto(f"{fixture_url}/other.html")
        refused = await browser.goto(other_site.url())
        assert "its site is on the deny list" in refused
    both = replace(
        settings,
        allow_sites=(f"a.localhost:{port_of(fixture_url)}", "*.localhost"),
        deny_sites=(f"a.localhost:{port_of(fixture_url)}",),
    )
    async with Browser(both) as browser:
        result = await browser.goto(f"{site_a}/other.html")
    assert "its site is on the deny list" in result
    await settle()
    assert other_site.hits == []


async def test_under_an_allow_list_localhost_and_127_0_0_1_stay_reachable_unless_denied(
    settings: Settings, fixture_url: str, other_site: OtherSite
):
    listed = replace(settings, allow_sites=("example.test",))
    async with Browser(listed) as browser:
        assert "at " + fixture_url in await browser.goto(f"{fixture_url}/other.html")
        local = fixture_url.replace("127.0.0.1", "localhost")
        assert "at " + local in await browser.goto(f"{local}/other.html")
        refused = await browser.goto(other_site.url("b.localhost"))
        assert "its site is not on the allow list" in refused
    denied = replace(listed, deny_sites=(f"127.0.0.1:{port_of(fixture_url)}",))
    async with Browser(denied) as browser:
        assert "its site is on the deny list" in await browser.goto(f"{fixture_url}/other.html")
    assert other_site.hits == []


async def test_an_empty_allow_list_means_no_limit(settings: Settings, site_a: str, site_b: str, other_site: OtherSite):
    async with Browser(settings) as browser:
        assert "at " + site_a in await browser.goto(f"{site_a}/other.html")
        assert "at " + site_b in await browser.goto(f"{site_b}/")
    assert other_site.hits[0] == "/"


async def test_file_urls_are_refused_unless_allowed(settings: Settings, tmp_path: Path):
    page = tmp_path / "local.html"
    page.write_text("<!doctype html><title>Local file</title><h1>Local file content</h1>")
    url = page.as_uri()
    async with Browser(settings) as browser:
        result = await browser.goto(url)
        snapshot = await browser.snapshot()
    assert "file:// pages are off, because allow_file_urls is false" in result
    assert "Blocked by site limits: " + url in snapshot
    assert "Local file content" not in snapshot
    async with Browser(replace(settings, allow_file_urls=True)) as browser:
        await browser.goto(url)
        assert "Local file content" in await browser.snapshot()


async def test_a_refused_load_is_in_the_network_list_as_a_403_and_a_refused_subresource_as_failed(
    allow_a: Settings, site_a: str, site_b: str
):
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        await browser.run(f"await page.evaluate(() => fetch({site_b + '/sub'!r}).catch(() => 0)); return 1;")
        await browser.goto(f"{site_b}/doc")
        text = ""
        for _ in range(50):
            text = await browser.call("network", all=True)
            if "/doc" in text and "/sub" in text:
                break
            await settle(0.1)
        refused_doc = [line for line in text.splitlines() if line.startswith("[r") and line.endswith("/doc")]
        refused_sub = [line for line in text.splitlines() if line.startswith("[r") and "/sub" in line]
    assert re.fullmatch(r"\[r\d+\] GET 403 \d+ms " + re.escape(site_b) + "/doc", refused_doc[0]), refused_doc
    assert "failed" in refused_sub[0] and "ERR_BLOCKED_BY_CLIENT" in refused_sub[0], refused_sub


# --- Enforcement without the debugging group, and the code ---------------------------------


async def test_enforcement_stays_on_when_the_debugging_group_is_off(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    core_only = replace(allow_a, groups=("core",))
    async with Browser(core_only) as browser:
        assert not browser.session.capturing
        names = {tool.name for tool in (await browser.client.list_tools()).tools}
        assert names == {"snapshot", "run"}
        await browser.goto(f"{site_a}/other.html")
        result = await browser.goto(f"{site_b}/x")
        await browser.goto(f"{site_a}/other.html")
        popup = await browser.run(
            "await page.evaluate(() => { const b = document.createElement('button'); b.id = 'b';"
            f" b.onclick = () => window.open({site_b + '/p'!r}); document.body.append(b); }});"
            " await page.locator('#b').click(); await page.waitForTimeout(500); return 1;"
        )
    assert "Refused by site limits: " + site_b + "/x" in result
    assert "Refused by site limits: " + site_b + "/p" in popup
    await settle()
    assert other_site.hits == []


def test_the_sdk_domain_policy_is_not_used():
    source = Path(__file__).parent.parent / "src" / "browser_tools_plugin"
    text = "\n".join(path.read_text() for path in source.glob("*.py"))
    assert "set_domain_policy" not in text and "setDomainPolicy" not in text


# --- The gaps ------------------------------------------------------------------------------


async def test_a_tab_created_directly_on_a_refused_site_is_closed(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    """Known gap, documented: the first request of such a tab leaves before the tab is closed."""
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        connection = browser.session._connection
        assert connection is not None
        await connection.send("Target.createTarget", {"url": f"{site_b}/direct"})
        for _ in range(50):
            targets = (await connection.send("Target.getTargets"))["targetInfos"]
            if not any(t["type"] == "page" and "/direct" in t["url"] for t in targets):
                break
            await settle(0.1)
        notice = await browser.run("return 1")
    assert not any(t["type"] == "page" and "b.localhost" in t["url"] for t in targets), targets
    assert "a tab on " + site_b + "/direct was closed" in notice
    # The gap, as measured in Chrome 157: the first request left before the tab was closed.
    assert other_site.hits == ["/direct"]


async def test_a_websocket_to_a_refused_site_is_not_checked(allow_a: Settings, site_a: str):
    """Known gap, documented: `Fetch` does not see WebSockets, so this handshake gets through."""
    seen: list[str] = []

    async def handler(connection: websockets.ServerConnection) -> None:
        seen.append(connection.request.path if connection.request else "")

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Browser(allow_a) as browser:
            await browser.goto(f"{site_a}/other.html")
            result = await browser.run(
                f"return await page.evaluate(() => new Promise(r => {{ const s = new WebSocket('ws://b.localhost:{port}/ws');"
                " s.onopen = () => r('open'); s.onerror = () => r('error'); }));"
            )
    assert result.startswith("open"), result  # documents the gap; if this fails, WebSockets are now refused
    assert seen == ["/ws"]


# --- A lost enforcement connection ---------------------------------------------------------


async def test_a_dropped_enforcement_connection_counts_as_a_lost_browser_and_the_next_call_relaunches(
    allow_a: Settings, site_a: str, site_b: str, other_site: OtherSite
):
    async with Browser(allow_a) as browser:
        await browser.goto(f"{site_a}/other.html")
        connection = browser.session._connection
        assert connection is not None
        await connection.close()  # as if the connection had dropped
        text, is_error = await browser.raw("snapshot")
        assert is_error
        assert text.startswith("The browser closed unexpectedly (the connection to the browser dropped). ")
        assert "call the tool again to start a fresh browser" in text
        assert not browser.session.launched
        # The loss is not latched: the next call starts a fresh browser, and the limits hold in it.
        assert "at " + site_a in await browser.goto(f"{site_a}/other.html")
        assert "Refused by site limits: " in await browser.goto(f"{site_b}/after")
    await settle()
    assert other_site.hits == []
