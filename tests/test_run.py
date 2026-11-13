"""run: what it advertises, what it enforces, and what it does in a real headless Chrome."""

import re
import time
from collections.abc import AsyncIterator

import pytest
from mcp import Client
from mcp_types import CallToolResult, TextContent
from stagehand import local_browser

from browser_tools_plugin.facade import BrowserSession, upstream_tool
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import Settings
from tests.conftest import find_chrome, needs_chrome

NO_BROWSER = pytest.mark.usefixtures("launch_forbidden")


@pytest.fixture
def launch_forbidden(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def launch(**_kwargs: object) -> None:
        calls.append("launch")
        raise AssertionError("the browser must not launch for this request")

    monkeypatch.setattr(local_browser, "launch", launch)
    return calls


def text_of(result: CallToolResult) -> str:
    (part,) = result.content
    assert isinstance(part, TextContent)
    return part.text


# -- What run advertises --------------------------------------------------------------------


@NO_BROWSER
async def test_run_keeps_upstreams_argument_schema_and_annotations(tmp_path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "run")
    assert tool.input_schema == upstream_tool("run")["inputSchema"]
    assert tool.output_schema is None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.open_world_hint is True


@NO_BROWSER
async def test_description_states_the_limit_and_quiet_wait_and_promises_nothing_that_fails(tmp_path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "run")
    description = tool.description or ""
    assert "stopped after 45 s" in description
    assert "ends early once the page has been quiet" in description
    for promise in ("mock", "route", "cdp", "listener", "download", "intercept"):
        assert promise not in description.lower()
    assert "./stagehand-artifacts" not in description
    assert "size-constrained" not in description


@NO_BROWSER
async def test_description_follows_the_settings(tmp_path):
    settings = Settings(data_dir=tmp_path, run_timeout=12, quiet_wait=False, result_ceiling=500)
    async with Client(create_server(settings)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "run")
    description = tool.description or ""
    assert "stopped after 12 s" in description
    assert "quiet" not in description
    assert "above 500 characters" in description


# -- What run enforces: the server checks the schema it advertises --------------------------

INVALID_ARGUMENTS = {
    "both code and actions": {"code": "return 1", "actions": [{"op": "click", "id": "0-1"}]},
    "neither": {},
    "empty code": {"code": ""},
    "code is not a string": {"code": 5},
    "empty actions": {"actions": []},
    "actions is not a list": {"actions": {"op": "click", "id": "0-1"}},
    "unknown op": {"actions": [{"op": "drag", "id": "0-1"}]},
    "kind instead of op": {"actions": [{"kind": "click", "id": "0-1"}]},
    "ref instead of id": {"actions": [{"op": "click", "ref": "0-1"}]},
    "id is not a string": {"actions": [{"op": "click", "id": 5}]},
    "missing value": {"actions": [{"op": "fill", "id": "0-1"}]},
    "extra action field": {"actions": [{"op": "click", "id": "0-1", "force": True}]},
    "negative delay": {"actions": [{"op": "type", "id": "0-1", "text": "a", "delay": -1}]},
    "empty select values": {"actions": [{"op": "select", "id": "0-1", "values": []}]},
    "empty id": {"actions": [{"op": "click", "id": ""}]},
    "unknown argument": {"code": "return 1", "timeout": 5},
}


@NO_BROWSER
@pytest.mark.parametrize("arguments", INVALID_ARGUMENTS.values(), ids=INVALID_ARGUMENTS.keys())
async def test_invalid_arguments_are_tool_errors_and_never_launch_the_browser(
    arguments: dict[str, object], launch_forbidden: list[str], tmp_path
):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        result = await client.call_tool("run", arguments)
    assert result.is_error
    assert text_of(result)
    assert launch_forbidden == []


@NO_BROWSER
async def test_a_bad_action_shape_says_what_an_action_needs(tmp_path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        result = await client.call_tool("run", INVALID_ARGUMENTS["kind instead of op"])
    assert 'Each action needs "op"' in text_of(result)


@NO_BROWSER
async def test_both_or_neither_says_exactly_one_is_required(tmp_path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        both = await client.call_tool("run", INVALID_ARGUMENTS["both code and actions"])
        neither = await client.call_tool("run", {})
    assert text_of(both) == text_of(neither) == "run requires exactly one of code or actions"


@NO_BROWSER
async def test_snapshot_rejects_arguments_its_schema_forbids(tmp_path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        extra = await client.call_tool("snapshot", {"scope": "main"})
        wrong_type = await client.call_tool("snapshot", {"includeIframes": "yes"})
    assert extra.is_error
    assert wrong_type.is_error


# -- Against a real browser -----------------------------------------------------------------


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(settings)
    try:
        yield session
    finally:
        await session.close()


def connected(session: BrowserSession) -> Client:
    return Client(create_server(session=session))


async def run_text(client: Client, **arguments: object) -> str:
    result = await client.call_tool("run", arguments)
    assert not result.is_error, result.content
    return text_of(result)


async def run_error(client: Client, **arguments: object) -> str:
    result = await client.call_tool("run", arguments)
    assert result.is_error, result.content
    return text_of(result)


async def snapshot_text(client: Client) -> str:
    result = await client.call_tool("snapshot", {})
    assert not result.is_error, result.content
    return text_of(result)


def element_id(snapshot: str, pattern: str) -> str:
    match = re.search(rf"\[(\d+-\d+)\] {pattern}", snapshot)
    assert match, snapshot
    return match.group(1)


@needs_chrome
async def test_actions_fill_and_click_by_element_id_and_return_completed_and_url(
    session: BrowserSession, fixture_url: str
):
    async with connected(session) as client:
        url = f"{fixture_url}/page.html"
        await run_text(client, code=f"await page.goto({url!r})")
        tree = await snapshot_text(client)
        actions = [
            {"op": "fill", "id": element_id(tree, "textbox: Name"), "value": "Ada"},
            {"op": "click", "id": element_id(tree, "button: Greet")},
        ]
        assert await run_text(client, actions=actions) == f'{{"completed":2,"url":"{url}"}}'
        assert await run_text(client, code="return await page.locator('#out').textContent()") == "Hello Ada"


@needs_chrome
async def test_page_code_returns_strings_as_is_and_other_values_as_compact_json(session: BrowserSession):
    async with connected(session) as client:
        assert await run_text(client, code="return 'plain \"text\"'") == 'plain "text"'
        assert (
            await run_text(client, code="return {a: 1, b: [true, null], c: 'é'}") == '{"a":1,"b":[true,null],"c":"é"}'
        )
        assert await run_text(client, code="return 42") == "42"
        assert await run_text(client, code="return []") == "[]"


@needs_chrome
async def test_page_code_without_a_value_says_so_and_gives_the_page_url(session: BrowserSession, fixture_url: str):
    async with connected(session) as client:
        url = f"{fixture_url}/page.html"
        await run_text(client, code=f"await page.goto({url!r})")
        expected = f"Done. No value returned. {url}"
        assert await run_text(client, code="1 + 1") == expected
        assert await run_text(client, code="return undefined") == expected
        assert await run_text(client, code="return null") == expected


@needs_chrome
async def test_a_stale_element_id_gives_upstreams_error_text(session: BrowserSession, fixture_url: str):
    async with connected(session) as client:
        await run_text(client, code=f"await page.goto({fixture_url + '/page.html'!r})")
        await snapshot_text(client)
        error = await run_error(client, actions=[{"op": "click", "id": "9-99"}])
        assert error == 'Snapshot ID "9-99" is stale or not actionable; call snapshot again.'


@needs_chrome
async def test_actions_before_any_snapshot_give_upstreams_error_text(session: BrowserSession, fixture_url: str):
    async with connected(session) as client:
        # A fresh page has no snapshot of its own, whatever came before it.
        await run_text(client, code=f"await page.goto({fixture_url + '/page.html'!r})")
        await run_text(client, code="const p = await context.newPage(); await p.goto('about:blank')")
        error = await run_error(client, actions=[{"op": "click", "id": "0-1"}])
        assert error == "No hydrated snapshot exists for the active page; call snapshot first."


@needs_chrome
async def test_actions_after_navigation_give_upstreams_error_text(session: BrowserSession, fixture_url: str):
    async with connected(session) as client:
        await run_text(client, code=f"await page.goto({fixture_url + '/page.html'!r})")
        tree = await snapshot_text(client)
        await run_text(client, code=f"await page.goto({fixture_url + '/other.html'!r})")
        error = await run_error(client, actions=[{"op": "click", "id": element_id(tree, "button: Greet")}])
        assert error == "The active page navigated after its snapshot; call snapshot again."


@needs_chrome
async def test_page_errors_are_tool_errors_redacted_and_without_a_stack(session: BrowserSession):
    async with connected(session) as client:
        secrets = [
            "https://example.test/x?apiKey=SECRETQUERY1",
            "sk-abcdef0123456789",
            "bb_live_abcd0123456789",
            "AIza" + "a" * 35,
            "Bearer abcdefghijklmnop",
        ]
        code = f"function inner() {{ throw new Error({' '.join(secrets)!r}); }} inner();"
        error = await run_error(client, code=code)
        for leaked in ("SECRETQUERY1", "0123456789", "a" * 35, "abcdefghijklmnop"):
            assert leaked not in error
        assert error.count("[redacted]") == 5
        assert "\n    at " not in error


@needs_chrome
async def test_syntax_errors_and_unsupported_calls_are_tool_errors(session: BrowserSession):
    async with connected(session) as client:
        assert "Invalid JavaScript" in await run_error(client, code="return (")
        assert "does not implement" in await run_error(client, code="return page.noSuchMethod()")


@needs_chrome
async def test_the_next_call_works_after_a_failed_run(session: BrowserSession):
    async with connected(session) as client:
        await run_error(client, code="throw new Error('first')")
        assert await run_text(client, code="return 'second'") == "second"


@needs_chrome
async def test_a_run_above_the_result_ceiling_is_a_normal_result_with_head_and_notice(tmp_path):
    settings = Settings(data_dir=tmp_path, chrome_path=find_chrome(), result_ceiling=100)
    session = BrowserSession(settings)
    try:
        async with Client(create_server(session=session)) as client:
            result = await client.call_tool("run", {"code": "return 'x'.repeat(5000)"})
    finally:
        await session.close()
    assert not result.is_error
    text = text_of(result)
    head, _, notice = text.partition("\n\n")
    assert head == "x" * 100
    assert (
        "result was 5000 characters; the code did run; return less, for example pick fields, slice or count" in notice
    )


@needs_chrome
async def test_a_snapshot_above_the_result_ceiling_is_a_normal_result_with_head_and_notice(tmp_path, fixture_url):
    settings = Settings(data_dir=tmp_path, chrome_path=find_chrome(), result_ceiling=120)
    session = BrowserSession(settings)
    try:
        async with Client(create_server(session=session)) as client:
            await run_text(client, code=f"await page.goto({fixture_url + '/page.html'!r})")
            result = await client.call_tool("snapshot", {})
    finally:
        await session.close()
    assert not result.is_error
    text = text_of(result)
    head, _, notice = text.partition("\n\n[Cut:")
    assert head.startswith("Page: Fixture page | ")
    assert len(head) <= 120
    assert re.search(r"\d+ characters and \d+ elements", notice)
    assert "run code" in notice
    assert "Do not call snapshot again" in notice


@needs_chrome
async def test_quiet_wait_is_on_by_default(session: BrowserSession):
    async with connected(session) as client:
        started = time.monotonic()
        await run_text(client, code="await page.waitForTimeout(5000); return 1")
        assert time.monotonic() - started < 4


@needs_chrome
async def test_quiet_wait_can_be_switched_off(tmp_path):
    settings = Settings(data_dir=tmp_path, chrome_path=find_chrome(), quiet_wait=False)
    session = BrowserSession(settings)
    try:
        async with Client(create_server(session=session)) as client:
            started = time.monotonic()
            await run_text(client, code="await page.waitForTimeout(1500); return 1")
            assert time.monotonic() - started >= 1.5
    finally:
        await session.close()


@needs_chrome
async def test_a_run_past_its_deadline_is_stopped_with_the_agreed_wording(tmp_path):
    settings = Settings(data_dir=tmp_path, chrome_path=find_chrome(), run_timeout=1.5, quiet_wait=False)
    session = BrowserSession(settings)
    try:
        async with Client(create_server(session=session)) as client:
            await snapshot_text(client)  # launch first, so only the run is timed
            started = time.monotonic()
            error = await run_error(client, code="await new Promise(r => setTimeout(r, 20000)); return 1")
            assert time.monotonic() - started < 10
            assert error == (
                "Run exceeded 1.5 s and was stopped. Some of it may have taken effect; "
                "call snapshot to see the page before retrying. Split long work into smaller runs."
            )
            assert await run_text(client, code="return 'alive'") == "alive"
    finally:
        await session.close()


@needs_chrome
async def test_default_deadlines_are_45_s_for_a_run_and_30_s_for_a_capture(tmp_path, monkeypatch):
    from stagehand_middleware.facade import tools as upstream

    monkeypatch.setattr(upstream, "RUN_BATCH_TIMEOUT_MS", 60_000)  # restored after the test
    monkeypatch.setattr(upstream, "PAGE_CAPTURE_DEADLINE_S", 120.0)
    session = BrowserSession(Settings(data_dir=tmp_path, chrome_path=find_chrome()))
    try:
        async with Client(create_server(session=session)) as client:
            await snapshot_text(client)
        assert upstream.RUN_BATCH_TIMEOUT_MS == 45_000
        assert upstream.PAGE_CAPTURE_DEADLINE_S == 30.0
    finally:
        await session.close()


@needs_chrome
async def test_a_snapshot_past_its_deadline_is_a_tool_error(tmp_path):
    settings = Settings(data_dir=tmp_path, chrome_path=find_chrome(), capture_timeout=0.001)
    session = BrowserSession(settings)
    try:
        async with Client(create_server(session=session)) as client:
            result = await client.call_tool("snapshot", {})
    finally:
        await session.close()
    assert result.is_error
    assert "page.snapshot received no response within 1ms" in text_of(result)
