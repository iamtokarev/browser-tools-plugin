"""screenshot against a real headless Chrome: the default image, the ladder and the limits."""

import base64
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from mcp import Client
from mcp_types import CallToolResult, ImageContent, TextContent

from browser_tools_plugin.facade import BrowserSession
from browser_tools_plugin.screenshots import MAX_BASE64_BYTES, MAX_SIDE_PX, image_size
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import OtherSite, needs_chrome

pytestmark = needs_chrome

# A canvas of random pixels: a JPEG of it is large at any quality that reads well.
NOISE_PAGE = """
await page.setViewportSize({ width: 1900, height: 1400 });
await page.evaluate(() => {
  document.body.style.margin = '0';
  document.body.innerHTML = '<canvas id=c width=1900 height=1400></canvas>';
  const context = document.getElementById('c').getContext('2d');
  const image = context.createImageData(1900, 1400);
  const random = new Uint8Array(65536);
  crypto.getRandomValues(random);
  for (let i = 0; i < image.data.length; i += 4) {
    const n = i / 4;
    image.data[i] = random[n % 65536];
    image.data[i + 1] = random[(n * 3 + 1) % 65536];
    image.data[i + 2] = random[(n * 7 + 2) % 65536];
    image.data[i + 3] = 255;
  }
  context.putImageData(image, 0, 0);
});
return 1;
"""


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(settings)
    try:
        yield session
    finally:
        await session.close()


async def goto(client: Client, url: str) -> None:
    result = await client.call_tool("run", {"code": f"await page.goto({url!r}); return 1;"})
    assert not result.is_error, result.content


def parts(result: CallToolResult) -> tuple[str, ImageContent]:
    """The two parts of a screenshot result: the text first, then the image."""
    assert not result.is_error, result.content
    text, image = result.content
    assert isinstance(text, TextContent)
    assert isinstance(image, ImageContent)
    return text.text, image


def raw(image: ImageContent) -> bytes:
    return base64.b64decode(image.data, validate=True)


async def test_default_is_a_viewport_jpeg_q60_with_a_text_part_before_the_image(
    session: BrowserSession, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        text, image = parts(await client.call_tool("screenshot", {}))
    data = raw(image)
    width, height = image_size(data)
    assert image.mime_type == "image/jpeg"
    assert data[:2] == b"\xff\xd8"
    assert text == f"Screenshot: viewport, jpeg q60, {width}x{height}"
    assert len(image.data) <= MAX_BASE64_BYTES
    assert max(width, height) <= MAX_SIDE_PX


async def test_png_and_full_page_are_honoured(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        tall = await client.call_tool(
            "run", {"code": "await page.evaluate(() => { document.body.style.height = '1500px'; }); return 1;"}
        )
        assert not tall.is_error, tall.content
        viewport_text, viewport = parts(await client.call_tool("screenshot", {"type": "png"}))
        full_text, full = parts(await client.call_tool("screenshot", {"type": "png", "fullPage": True}))
    assert viewport_text.startswith("Screenshot: viewport, png, ")
    assert full.mime_type == "image/png"
    assert raw(full)[:8] == b"\x89PNG\r\n\x1a\n"
    assert full_text.startswith("Screenshot: full page, png, ")
    assert image_size(raw(full))[1] > image_size(raw(viewport))[1]


async def test_a_quality_is_used_and_named(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        text, _ = parts(await client.call_tool("screenshot", {"quality": 40.4}))
    assert text.startswith("Screenshot: viewport, jpeg q40, ")


async def test_a_large_image_goes_down_the_ladder_to_a_quality_that_fits(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        result = await client.call_tool("run", {"code": NOISE_PAGE})
        assert not result.is_error, result.content
        text, image = parts(await client.call_tool("screenshot", {"type": "png", "fullPage": True}))
    # The noise is above the limit as a PNG and at the first JPEG rungs, so a low quality was needed.
    assert text.startswith("Screenshot: viewport, jpeg q")
    assert text.endswith(", 1900x1400")
    assert int(text.split(" q")[1].split(",")[0]) <= 40
    assert len(image.data) <= MAX_BASE64_BYTES


async def set_pixel_ratio(session: BrowserSession, width: int, height: int, ratio: float) -> None:
    """Make the active page a `ratio` device pixel ratio page, as on a headed retina screen."""
    page = await session._active_page()
    await page.set_viewport_size(width, height, device_scale_factor=ratio)


async def test_a_2x_page_wider_than_the_limit_is_captured_again_scaled_down(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        await set_pixel_ratio(session, 1280, 720, 2)  # 2560x1440 device pixels
        text, image = parts(await client.call_tool("screenshot", {}))
    assert text == "Screenshot: viewport, jpeg q60, 1280x720, scaled down"
    assert image.mime_type == "image/jpeg"
    assert image_size(raw(image)) == (1280, 720)


async def test_a_2x_page_that_fits_keeps_its_sharp_pixels(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        await set_pixel_ratio(session, 900, 600, 2)
        text, image = parts(await client.call_tool("screenshot", {}))
    assert text == "Screenshot: viewport, jpeg q60, 1800x1200"
    assert image_size(raw(image)) == (1800, 1200)


async def test_a_window_wider_than_the_limit_is_scaled_down_to_the_limit(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        resized = await client.call_tool(
            "run", {"code": "await page.setViewportSize({ width: 2600, height: 2400 }); return 1;"}
        )
        assert not resized.is_error, resized.content
        text, image = parts(await client.call_tool("screenshot", {}))
    width, height = image_size(raw(image))
    assert text == f"Screenshot: viewport, jpeg q60, {width}x{height}, scaled down"
    assert max(width, height) <= MAX_SIDE_PX
    assert width / height == pytest.approx(2600 / 2400, abs=0.01)


async def test_a_full_page_taller_than_the_limit_falls_back_to_the_viewport(session: BrowserSession, fixture_url: str):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        tall = await client.call_tool(
            "run", {"code": "await page.evaluate(() => { document.body.style.height = '4000px'; }); return 1;"}
        )
        assert not tall.is_error, tall.content
        text, image = parts(await client.call_tool("screenshot", {"fullPage": True}))
    assert text.startswith("Screenshot: viewport, jpeg q60, ")
    assert max(image_size(raw(image))) <= MAX_SIDE_PX


async def test_an_image_that_no_rung_fits_is_a_tool_error_that_says_what_to_do(
    session: BrowserSession, monkeypatch: pytest.MonkeyPatch, fixture_url: str
):
    sizes: list[tuple[bool, str, int | None]] = []
    real = BrowserSession._shoot

    async def too_big(page, attempt):
        sizes.append((attempt.full_page, attempt.type, attempt.quality))
        shot = await real(page, attempt)
        return replace(shot, data=shot.data + bytes(800_000))  # base64 of this is above 1 MB

    monkeypatch.setattr(BrowserSession, "_shoot", staticmethod(too_big))
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        result = await client.call_tool("screenshot", {})
    assert sizes == [(False, "jpeg", 60), (False, "jpeg", 40), (False, "jpeg", 25), (False, "jpeg", 10)]
    assert result.is_error
    (part,) = result.content
    assert isinstance(part, TextContent)
    assert "1 MB of base64 or 2000 px" in part.text
    assert "setViewportSize" in part.text


async def test_a_page_blocked_by_site_limits_shows_the_blocked_page(
    settings: Settings, fixture_url: str, other_site: OtherSite
):
    port = fixture_url.rsplit(":", 1)[1]
    session = BrowserSession(replace(settings, allow_sites=(f"a.localhost:{port}",)))
    try:
        async with Client(create_server(session=session)) as client:
            await goto(client, f"http://a.localhost:{port}/page.html")
            await goto(client, other_site.url("b.localhost", "/secret"))
            snapshot = await client.call_tool("snapshot", {})
            text, image = parts(await client.call_tool("screenshot", {}))
    finally:
        await session.close()
    (snapshot_part,) = snapshot.content
    assert isinstance(snapshot_part, TextContent)
    assert snapshot_part.text.startswith("Page: Blocked by site limits | ")
    assert text.startswith("Screenshot: viewport, jpeg q60, ")
    assert image_size(raw(image))[0] > 100
    assert other_site.hits == []


async def test_page_code_saves_a_screenshot_only_inside_the_artifact_directory(
    session: BrowserSession, settings: Settings, fixture_url: str
):
    async with Client(create_server(session=session)) as client:
        await goto(client, f"{fixture_url}/page.html")
        saved = await client.call_tool("run", {"code": "await page.screenshot({ path: 'shot.png' }); return 1;"})
        escaped = await client.call_tool("run", {"code": "await page.screenshot({ path: '../escape.png' }); return 1;"})
    assert not saved.is_error, saved.content
    assert (settings.artifact_dir / "shot.png").read_bytes()[:4] == b"\x89PNG"
    assert escaped.is_error
    assert not (settings.artifact_dir.parent / "escape.png").exists()


async def test_arguments_outside_upstreams_schema_are_tool_errors(session: BrowserSession):
    async with Client(create_server(session=session)) as client:
        for arguments in ({"type": "gif"}, {"quality": 101}, {"scale": 2}):
            result = await client.call_tool("screenshot", arguments)
            assert result.is_error, arguments
        assert not session.launched
