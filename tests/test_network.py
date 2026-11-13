"""The network tool and the request capture behind it."""

import asyncio
import re
import socket
from collections.abc import AsyncIterator
from typing import cast

import pytest
from mcp import Client
from mcp_types import TextContent

from browser_tools_plugin.capture import PageBuffer
from browser_tools_plugin.console_text import PageRef
from browser_tools_plugin.facade import BrowserSession, sanitize_error_message
from browser_tools_plugin.network_text import (
    BODY_SHOW_LIMIT,
    BodyCache,
    BodyNotHeldError,
    Request,
    mask_header,
    render_detail,
    render_report,
    request_line,
)
from browser_tools_plugin.results import cap_report
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import needs_chrome

PAGE = PageRef("Fixture", "http://127.0.0.1/")


def make(number: int = 1, **fields: object) -> Request:
    values: dict[str, object] = {
        "id": f"r{number}",
        "method": "GET",
        "url": f"http://127.0.0.1/api/{number}",
        "resource_type": "Fetch",
        "session": "s",
        "cdp_id": str(number),
        "start": 0.0,
    }
    values.update(fields)
    return Request(**values)  # ty: ignore[invalid-argument-type]


def done(number: int = 1, status: int = 200, **fields: object) -> Request:
    values: dict[str, object] = {
        "status": status,
        "finished": True,
        "duration": 0.132,
        "mime_type": "application/json",
    }
    return make(number, **{**values, **fields})


def plain(text: str) -> str:
    return text


# --- Lines, filters and the buffer, without a browser --------------------------------------


def test_a_line_is_id_method_status_time_and_url_and_never_a_body():
    request = done(12, 404, url="https://example.test/api/x")
    request.body = '{"secret":"body"}'
    assert request_line(request, plain) == "[r12] GET 404 132ms https://example.test/api/x"


def test_a_pending_and_a_failed_request_show_their_state():
    assert request_line(make(3), plain) == "[r3] GET pending - http://127.0.0.1/api/3"
    failed = make(4, failure="net::ERR_CONNECTION_REFUSED", duration=0.003, finished=True)
    assert request_line(failed, plain) == "[r4] GET failed 3ms http://127.0.0.1/api/4 (net::ERR_CONNECTION_REFUSED)"


def test_a_long_url_is_cut_at_300_characters():
    line = request_line(done(url="http://h/" + "u" * 400), plain)
    assert line.endswith("…(+109 chars)")


def test_the_buffer_keeps_the_last_1000_requests_and_counts_the_dropped():
    buffer = PageBuffer("t")
    removed = [r for i in range(1, 1201) for r in buffer.add_request(done(i))]
    assert len(buffer.requests) == 1000
    assert [r.id for r in removed] == [f"r{i}" for i in range(1, 201)]
    assert all(r.released for r in removed)
    report = buffer.read_requests(all=True, failed=False, url=None, types="default")
    assert report.dropped == 200
    assert report.matching == 1000
    assert report.shown[0].id == "r1200"  # newest first
    assert len(report.shown) == 100
    assert "200 older requests were dropped from this page's buffer, which keeps the last 1000." in render_report(
        report, plain
    )


def test_new_requests_are_returned_once_and_all_returns_everything():
    buffer = PageBuffer("t")
    for i in range(1, 4):
        buffer.add_request(done(i))
    read = lambda **kw: buffer.read_requests(all=kw.get("all", False), failed=False, url=None, types="default")  # noqa: E731
    assert [r.id for r in read().shown] == ["r3", "r2", "r1"]
    again = read()
    assert again.shown == []
    assert "Nothing new. The buffer holds 3 requests; pass all: true to read them again." in render_report(again, plain)
    buffer.add_request(done(4))
    assert [r.id for r in read().shown] == ["r4"]
    assert len(read(all=True).shown) == 4


def test_a_second_call_returns_the_older_requests_that_did_not_fit():
    buffer = PageBuffer("t")
    for i in range(1, 131):
        buffer.add_request(done(i))
    first = buffer.read_requests(all=False, failed=False, url=None, types="default")
    assert (len(first.shown), first.matching) == (100, 130)
    assert "30 older new requests are not shown. Call network again to read them." in render_report(first, plain)
    second = buffer.read_requests(all=False, failed=False, url=None, types="default")
    assert [r.id for r in second.shown][:1] == ["r30"]
    assert len(second.shown) == 30


def test_failed_means_status_400_and_up_or_no_answer_and_a_hidden_request_stays_new():
    buffer = PageBuffer("t")
    buffer.add_request(done(1, 200))
    buffer.add_request(done(2, 404))
    buffer.add_request(done(3, 500))
    buffer.add_request(make(4, failure="net::ERR_FAILED", finished=True))
    buffer.add_request(done(5, 302))
    failed = buffer.read_requests(all=False, failed=True, url=None, types="default")
    assert [r.id for r in failed.shown] == ["r4", "r3", "r2"]
    rest = buffer.read_requests(all=False, failed=False, url=None, types="default")
    assert [r.id for r in rest.shown] == ["r5", "r1"]  # the failed ones were read already


def test_url_is_a_case_insensitive_substring():
    buffer = PageBuffer("t")
    buffer.add_request(done(1, url="http://h/Api/Users"))
    buffer.add_request(done(2, url="http://h/other"))
    report = buffer.read_requests(all=True, failed=False, url="api/users", types="default")
    assert [r.id for r in report.shown] == ["r1"]
    assert "with a URL that contains 'api/users'" in render_report(report, plain)


def test_images_fonts_media_and_stylesheets_are_hidden_unless_types_is_all():
    buffer = PageBuffer("t")
    buffer.add_request(done(1))
    for number, kind in enumerate(("Image", "Font", "Media", "Stylesheet"), start=2):
        buffer.add_request(done(number, resource_type=kind))
    buffer.add_request(done(6, 404, resource_type="Image"))
    default = buffer.read_requests(all=True, failed=False, url=None, types="default")
    assert [r.id for r in default.shown] == ["r1"]
    assert (default.hidden, default.hidden_failed) == (5, 1)
    text = render_report(default, plain)
    assert '5 image, font, media or stylesheet requests are hidden, 1 of them failed. Pass types: "all"' in text
    everything = buffer.read_requests(all=True, failed=False, url=None, types="all")
    assert len(everything.shown) == 6
    assert everything.hidden == 0


def test_secret_header_values_show_only_their_length():
    for name in ("Authorization", "cookie", "Set-Cookie", "PROXY-AUTHORIZATION"):
        assert mask_header(name, "Bearer FAKE-VALUE", plain) == [f"{name}: [redacted, 17 chars]"]
    assert mask_header("Set-Cookie", "a=1\nb=22", plain) == [
        "Set-Cookie: [redacted, 3 chars]",
        "Set-Cookie: [redacted, 4 chars]",
    ]
    assert mask_header("Accept", "*/*", plain) == ["Accept: */*"]


# --- The body cache and the detail, without a browser --------------------------------------


def test_the_cache_drops_the_oldest_body_first_when_it_is_full():
    cache = BodyCache(limit=100)
    requests = [done(i) for i in range(1, 5)]
    for request in requests:
        cache.add(request, "x" * 40, 40)
    assert [r.body is not None for r in requests] == [False, False, True, True]
    assert cache.bytes_held == 80
    assert "body cache, which is full" in requests[0].body_note
    cache.discard(requests[3])
    assert cache.bytes_held == 40


def test_a_body_is_cut_at_10_kb_with_its_full_size_stated():
    request = done(1)
    request.body = "z" * 25000
    text = render_detail(request, PAGE, plain)
    assert "Response body (25000 bytes; the first 10240 are shown, 14760 are cut):" in text
    assert text.endswith("z" * BODY_SHOW_LIMIT)


def test_a_small_body_is_shown_whole_with_its_size():
    request = done(1)
    request.body = '{"a":1}'
    assert render_detail(request, PAGE, plain).endswith('Response body (7 bytes):\n{"a":1}')


def test_a_binary_body_gives_size_and_type_only():
    request = make(1, status=200, finished=True, mime_type="image/png", size=2048)
    text = render_detail(request, PAGE, plain)
    assert "Response body: not shown, because it is not text. Type: image/png; size: 2048 bytes." in text


def test_a_text_body_that_is_gone_is_an_error_that_says_to_reproduce_the_request():
    request = done(7, body_note="Chrome no longer had it when the request finished")
    with pytest.raises(BodyNotHeldError) as raised:
        render_detail(request, PAGE, plain)
    message = str(raised.value)
    assert "The body of r7 is not held: Chrome no longer had it when the request finished." in message
    assert "reproduce the request and call network again" in message
    assert "Request r7 on page:" in message  # the headers are still there


def test_headers_and_bodies_are_masked_in_the_detail():
    request = done(
        1,
        request_headers={"Authorization": "Bearer FAKETOKEN0123456789", "Accept": "*/*", "Cookie": "a=1; b=2"},
        response_headers={"Set-Cookie": "session=FAKE; Path=/", "X-Key": "?token=FAKEVALUE"},
        post_data='{"key":"sk-FAKEKEY123456789"}',
    )
    request.body = '{"note":"Bearer FAKEBEARER123456789"}'
    text = render_detail(request, PAGE, sanitize_error_message)
    assert "Authorization: [redacted, 26 chars]" in text
    assert "Cookie: [redacted, 8 chars]" in text
    assert "Set-Cookie: [redacted, 20 chars]" in text
    assert "Accept: */*" in text
    assert "FAKETOKEN" not in text
    assert "FAKESESSION" not in text
    assert 'Request body (29 bytes):\n{"key":"sk-FAKEKE[redacted]"}' in text
    assert "Bearer [redacted]" in text
    assert "FAKEBEARER" not in text


def test_a_secret_at_the_cut_of_a_long_body_is_still_masked():
    request = done(1)
    request.body = "a" * (BODY_SHOW_LIMIT - 21) + " sk-FAKEKEY1234567890123456789 tail"
    text = render_detail(request, PAGE, sanitize_error_message)
    assert "sk-FAKEKE[redacted]" in text
    assert "FAKEKEY1234" not in text


def test_a_pending_a_failed_and_a_redirected_request_have_no_body():
    assert "none yet" in render_detail(make(1), PAGE, plain)
    failed = make(2, failure="net::ERR_FAILED", finished=True)
    assert "none, because the request failed" in render_detail(failed, PAGE, plain)
    redirect = done(3, 302, redirected=True)
    assert "none, because the response was a redirect" in render_detail(redirect, PAGE, plain)


# --- The result ceiling ---------------------------------------------------------------------


def test_a_report_above_the_ceiling_keeps_whole_lines_and_says_what_was_cut():
    text = "\n".join(f"line {i:03d}" for i in range(50))
    result = cap_report(text, 100, "Narrow the call", listing=True)
    head, _, notice = result.partition("\n\n[Cut:")
    assert head == "\n".join(f"line {i:03d}" for i in range(11))
    assert f"{len(text) - len(head)} characters in 39 lines are missing from the end of this result" in notice
    assert "count as read" in notice
    assert notice.endswith("Narrow the call.]")
    assert cap_report("short", 100, "x", listing=True) == "short"


class StubSession:
    """A session whose console and network answers are fixed, to test the server's ceiling."""

    def __init__(self, settings: Settings, text: str) -> None:
        self.settings = settings
        self._text = text

    async def console(self, **_: object) -> str:
        return self._text

    async def network(self, **_: object) -> str:
        return self._text

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("tool", ["console", "network"])
async def test_console_and_network_output_goes_through_the_result_ceiling(tool: str, tmp_path):
    settings = Settings(data_dir=tmp_path, result_ceiling=500)
    text = "header\n" + "\n".join(f"line {i:04d} " + "y" * 50 for i in range(100))
    server = create_server(session=cast(BrowserSession, StubSession(settings, text)))
    async with Client(server) as client:
        result = await client.call_tool(tool, {})
    assert not result.is_error
    (part,) = result.content
    assert isinstance(part, TextContent)
    head, _, notice = part.text.partition("\n\n[Cut:")
    assert len(head) <= 500
    assert head.startswith("header\nline 0000")
    assert "are missing from the end of this result" in notice
    assert f"Call {tool} again" in notice


# --- The tool, against a real Chrome -------------------------------------------------------


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(settings)
    try:
        yield session
    finally:
        await session.close()


async def raw_call(client: Client, name: str, **arguments: object) -> tuple[str, bool]:
    result = await client.call_tool(name, arguments)
    (part,) = result.content
    assert isinstance(part, TextContent)
    return part.text, bool(result.is_error)


async def call(client: Client, name: str, **arguments: object) -> str:
    text, is_error = await raw_call(client, name, **arguments)
    assert not is_error, text
    return text


async def goto(client: Client, url: str) -> None:
    await call(client, "run", code=f"await page.goto({url!r}); return 1;")


def closed_port() -> int:
    """A port on 127.0.0.1 that refuses connections: one that was free a moment ago."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def load_network_page(client: Client, base: str) -> None:
    await goto(client, f"{base}/network.html?refused={closed_port()}")
    await call(
        client,
        "run",
        code=(
            "for (let i = 0; i < 100 && !(await page.evaluate(() => window.requestsDone)); i++) "
            "await page.waitForTimeout(100); return 1;"
        ),
    )


def lines_of(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"\[r\d+\] ", line)]


def id_of(text: str, needle: str) -> str:
    """The ID of the oldest request whose line holds `needle`; the list is newest first."""
    line = [line for line in lines_of(text) if needle in line][-1]
    return line[1 : line.index("]")]


async def network_until(client: Client, needle: str, **arguments: object) -> str:
    """Network output that holds `needle`, polling because the events come after the call returns."""
    seen = ""
    for _ in range(50):
        seen += "\n" + await call(client, "network", **arguments)
        if needle in seen:
            return seen
        await asyncio.sleep(0.1)
    pytest.fail(f"{needle!r} never appeared in:\n{seen}")


@needs_chrome
async def test_the_list_has_lines_of_the_documented_shape_and_a_header(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        text = await network_until(client, "/refused")
    assert text.splitlines()[1].startswith("Network of page: Network fixture | ")
    lines = lines_of(text)
    assert any(re.fullmatch(rf"\[r\d+\] GET 200 \d+ms {re.escape(fixture_url)}/api/items", line) for line in lines)
    assert any(re.fullmatch(rf"\[r\d+\] GET 404 \d+ms {re.escape(fixture_url)}/api/missing", line) for line in lines)
    assert any(re.fullmatch(rf"\[r\d+\] POST 200 \d+ms {re.escape(fixture_url)}/api/echo", line) for line in lines)
    assert any(
        re.fullmatch(r"\[r\d+\] GET failed \d+ms http://127\.0\.0\.1:\d+/refused \(net::ERR_CONNECTION_REFUSED\)", line)
        for line in lines
    )
    assert any(re.fullmatch(rf"\[r\d+\] GET 302 \d+ms {re.escape(fixture_url)}/api/redirect", line) for line in lines)
    assert any(f"{fixture_url}/network.html?refused=" in line for line in lines)
    assert not any("hello" in line or "FAKE" in line for line in lines)  # no bodies, no secrets
    ids = [int(line[2 : line.index("]")]) for line in lines]
    assert ids == sorted(ids, reverse=True)  # newest first
    # The image and the stylesheet are hidden, and the header says so.
    assert "pixel.png" not in text
    assert re.search(r"2 image, font, media or stylesheet requests are hidden\. Pass types: \"all\"", text)


@needs_chrome
async def test_a_call_returns_only_what_is_new_and_all_returns_the_buffer(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        await network_until(client, "/refused")
        again = await call(client, "network")
        assert "0 new requests" in again
        assert lines_of(again) == []
        await call(client, "run", code="await page.evaluate(() => fetch('/api/items?again=1')); return 1;")
        fresh = await network_until(client, "again=1")
        assert len(lines_of(fresh)) == 1
        everything = await call(client, "network", all=True)
    assert len(lines_of(everything)) >= 10
    assert f"{fixture_url}/api/items?again=1" in lines_of(everything)[0]


@needs_chrome
async def test_failed_url_and_types_filters(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        await network_until(client, "/refused", all=True)
        failed = await call(client, "network", all=True, failed=True)
        by_url = await call(client, "network", all=True, url="API/ITEMS")
        every_type = await call(client, "network", all=True, types="all")
    assert sorted(re.sub(r"^\[r\d+\] GET (\S+) .*/([\w.]+)( \(.*)?$", r"\1 \2", line) for line in lines_of(failed)) == [
        "404 favicon.ico",  # Chrome asks for it by itself
        "404 missing",
        "failed refused",
    ]
    assert "that failed" in failed
    assert len(lines_of(by_url)) == 2  # the fetch, and the redirect that ends there
    assert all(line.endswith("/api/items") for line in lines_of(by_url))
    assert any("pixel.png" in line and " 200 " in line for line in lines_of(every_type))
    assert any("style.css" in line for line in lines_of(every_type))


@needs_chrome
async def test_a_request_by_id_gives_headers_and_both_bodies_with_secrets_masked(
    session: BrowserSession, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        listing = await network_until(client, "/refused")
        echo = await call(client, "network", id=id_of(listing, "/api/echo"))
        secret = await call(client, "network", id=id_of(listing, "/api/secret"))
        missing = await call(client, "network", id=id_of(listing, "/api/missing"))
    assert re.search(r"^POST http://127\.0\.0\.1:\d+/api/echo$", echo, re.M)
    assert "Status: 200 OK; time: " in echo
    assert re.search(r"^  [Aa]uthorization: \[redacted, 26 chars\]$", echo, re.M)
    assert re.search(r"^  [Cc]ookie: \[redacted, 20 chars\]$", echo, re.M)  # fake=FAKECOOKIEVALUE
    assert "FAKETOKEN" not in echo and "FAKECOOKIE" not in echo
    assert re.search(r"^  [Cc]ontent-[Tt]ype: application/json$", echo, re.M)
    assert 'Request body (17 bytes):\n{"hello":"world"}' in echo
    assert 'Response body (26 bytes):\n{"echo":{"hello":"world"}}' in echo
    assert re.search(r"^  [Ss]et-[Cc]ookie: \[redacted, 32 chars\]$", secret, re.M)
    assert "FAKESESSION" not in secret
    assert '"token":"sk-FAKEKE[redacted]"' in secret
    assert "Bearer [redacted]" in secret and "FAKEBEARER" not in secret
    assert "Status: 404 Not Found" in missing
    assert 'Response body (24 bytes):\n{"error":"no such item"}' in missing


@needs_chrome
async def test_bodies_stay_readable_after_the_page_navigates_away(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        listing = await network_until(client, "/refused")
        ids = {name: id_of(listing, name) for name in ("/api/items", "/api/medium", "/api/bin", "/api/big")}
        await goto(client, f"{fixture_url}/other.html")
        items = await call(client, "network", id=ids["/api/items"])
        medium = await call(client, "network", id=ids["/api/medium"])
        binary = await call(client, "network", id=ids["/api/bin"])
        big, big_error = await raw_call(client, "network", id=ids["/api/big"])
        current = await call(client, "network")
    assert 'Response body (17 bytes):\n{"items":[1,2,3]}' in items
    assert "Response body (20000 bytes; the first 10240 are shown, 9760 are cut):" in medium
    assert medium.endswith("m" * 10240)
    assert (
        "Response body: not shown, because it is not text. Type: application/octet-stream; size: 2048 bytes." in binary
    )
    # 300 KB is above the 256 KB cache cap, and Chrome dropped it with the page.
    assert big_error
    assert "The body of " in big and "is not held" in big and "larger than 256 KB" in big
    assert "reproduce the request and call network again" in big
    assert "Request headers" in big
    # The tab keeps its buffer across the navigation; the old requests were read already.
    assert "Network of page: Other page | " in current
    assert lines_of(current) == [line for line in lines_of(current) if "/other.html" in line]


@needs_chrome
async def test_a_body_above_the_cache_cap_is_read_from_chrome_while_the_page_is_loaded(
    session: BrowserSession, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        listing = await network_until(client, "/refused")
        big = await call(client, "network", id=id_of(listing, "/api/big"))
    assert "Response body (300000 bytes; the first 10240 are shown, 289760 are cut):" in big
    assert big.endswith("b" * 10240)


@needs_chrome
async def test_the_buffer_keeps_1000_requests_per_page(session: BrowserSession, fixture_url: str):
    code = (
        "await page.evaluate(async () => { for (let i = 0; i < 1050; i += 10) await Promise.all("
        "Array.from({length: 10}, (_, j) => fetch('/api/items?n=' + (i + j)).then(r => r.text()))); }); return 1;"
    )
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/other.html")
        await call(client, "run", code=code)
        await network_until(client, "n=1049", all=True)
        text = ""
        for _ in range(50):
            text = await call(client, "network", all=True)
            if re.search(r"\d+ older requests were dropped from this page's buffer, which keeps the last 1000", text):
                break
            await asyncio.sleep(0.1)
    assert "100 of 1000 requests, newest first." in text
    assert re.search(r"\d+ older requests were dropped from this page's buffer, which keeps the last 1000", text)


@needs_chrome
async def test_a_request_of_another_page_is_found_by_id_and_listed_by_page(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await load_network_page(client, fixture_url)
        listing = await network_until(client, "/refused")
        items_id = id_of(listing, "/api/items")
        await call(
            client,
            "run",
            code="const p = await context.newPage(); await p.goto("
            + repr(fixture_url + "/other.html")
            + "); return 1;",
        )
        other = await network_until(client, "other.html")
        assert "Network of page: Other page | " in other
        assert "Other pages (name one in page): Network fixture | " in other
        first = await call(client, "network", page="network fixture", all=True)
        assert "Network of page: Network fixture | " in first
        assert f"[{items_id}] GET 200" in first
        assert "Response body" in await call(client, "network", id=items_id)  # from any page
        text, is_error = await raw_call(client, "network", page="no such page")
        assert is_error and "matches no open page" in text
        text, is_error = await raw_call(client, "network", id="r99999")
        assert is_error and "No page holds a request r99999" in text


@needs_chrome
async def test_iframe_and_popup_requests_are_captured_from_their_first_one(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/console.html")
        await call(
            client, "run", code="await page.locator('#popup').click(); await page.waitForTimeout(500); return 1;"
        )
        popup = await network_until(client, "/popup.html")
        first = await call(client, "network", page="console fixture", all=True)
    assert "Network of page: Popup fixture | " in popup  # the popup is the active page
    assert re.search(r"\[r\d+\] GET 200 \d+ms .*/popup\.html$", popup, re.M)
    assert re.search(r"GET 200 \d+ms .*/frame\.html$", first, re.M)  # the iframe's request is on its page
    assert re.search(r"GET 404 \d+ms .*/missing\.json$", first, re.M)


@needs_chrome
async def test_network_before_any_browser_does_not_launch_one(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        text = await call(client, "network")
    assert "No browser is open yet" in text
    assert not session.launched


@needs_chrome
async def test_a_busy_network_page_is_cut_by_the_result_ceiling(settings: Settings, fixture_url: str):
    session = BrowserSession(
        Settings(data_dir=settings.data_dir, chrome_path=settings.chrome_path, result_ceiling=1500)
    )
    code = (
        "await page.evaluate(async () => { for (let i = 0; i < 60; i++) "
        "await fetch('/api/items?pad=' + 'p'.repeat(250) + i); }); return 1;"
    )
    try:
        async with Client(create_server(session=session)) as client:
            await goto(client, f"{fixture_url}/other.html")
            await call(client, "run", code=code)
            await network_until(client, "pad=", all=True)
            text = await call(client, "network", all=True)
    finally:
        await session.close()
    assert len(text.partition("\n\n[Cut:")[0]) <= 1500
    assert "[Cut: " in text and "are missing from the end of this result" in text


@needs_chrome
async def test_a_busy_console_page_is_cut_by_the_result_ceiling(settings: Settings, fixture_url: str):
    session = BrowserSession(
        Settings(data_dir=settings.data_dir, chrome_path=settings.chrome_path, result_ceiling=1500)
    )
    try:
        async with Client(create_server(session=session)) as client:
            await goto(client, f"{fixture_url}/flood.html")
            text = ""
            for _ in range(50):
                text = await call(client, "console", all=True)
                if "[Cut: " in text:
                    break
                await asyncio.sleep(0.1)
    finally:
        await session.close()
    assert len(text.partition("\n\n[Cut:")[0]) <= 1500
    assert "Call console again with a higher level or with page" in text
