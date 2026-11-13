"""The facade seam: the only module that touches `stagehand` and `stagehand-middleware`.

Every adjustment to upstream behaviour lives here, so that upgrading either pinned
package, or replacing the middleware with direct SDK calls, changes this file only.
"""

import asyncio
import re
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from stagehand import Stagehand, local_browser
from stagehand.browser import _find_chrome_path
from stagehand.rpc_client import RPCError
from stagehand_middleware.facade import tools as _upstream_tools
from stagehand_middleware.facade.bundle import facade_tools
from stagehand_middleware.facade.tools import StagehandFacadeTools, sanitize_error_message

from . import console_text, network_text
from .capture import Capture
from .cdp import CdpConnection
from .limits import SiteEnforcer, refusal_notice
from .results import NO_VALUE, page_code_text
from .settings import Settings
from .sites import SiteLimits
from .targets import Targets

__all__ = [
    "BrowserLostError",
    "BrowserSession",
    "RunTimeoutError",
    "find_chrome",
    "sanitize_error_message",
    "upstream_tool",
]


def upstream_tool(name: str) -> dict[str, Any]:
    """Upstream's tool definition from the "explicit snapshot actions" set."""
    for tool in facade_tools("explicitSnapshotActions"):
        if tool["name"] == name:
            return deepcopy(tool)
    raise KeyError(name)


def find_chrome(explicit_path: str | None = None) -> str:
    """The Chrome that a launch would use: the explicit path, else the SDK's own search.

    Raises RuntimeError when there is none. Nothing is started.
    """
    return _find_chrome_path(explicit_path)


class RunTimeoutError(Exception):
    """A run went past its deadline and the middleware stopped it."""

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"Run exceeded {seconds:g} s and was stopped. Some of it may have taken effect; "
            "call snapshot to see the page before retrying. Split long work into smaller runs."
        )


class BrowserLostError(Exception):
    """The browser is gone. The next call starts a fresh one."""

    def __init__(self, cause: str) -> None:
        super().__init__(
            f"The browser closed unexpectedly ({cause}). Open tabs and page state are gone; "
            "call the tool again to start a fresh browser, then navigate back."
        )


def _check_run_arguments(code: str | None, actions: Sequence[Mapping[str, Any]] | None) -> None:
    """Upstream's own checks on run arguments, which would otherwise run only after a launch."""
    if (code is None) == (actions is None):
        raise _upstream_tools.StagehandFacadeInputError("run requires exactly one of code or actions")
    _upstream_tools._validate_run_input({"code": code} if actions is None else {"actions": list(actions)})


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class BrowserSession:
    """One local browser per server process, launched on the first call that needs it.

    One lock serialises every call, including the launch.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._tools: StagehandFacadeTools | None = None
        self._stagehand: Stagehand | None = None
        self._connection: CdpConnection | None = None
        self._capture: Capture | None = None
        self._enforcer: SiteEnforcer | None = None
        self._lost: str | None = None  # why the browser counts as lost, until the next call reports it
        self._closing = False

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def launched(self) -> bool:
        return self._tools is not None

    @property
    def capturing(self) -> bool:
        """Whether a launched browser is being captured: the debugging group was on at launch."""
        return self._capture is not None

    async def snapshot(self, *, include_iframes: bool = True) -> str:
        """The active page's tree, led by one `Page: <title> | <url>` line."""
        async with self._lock:
            await self._raise_if_lost()
            tools = await self._ensure_launched()
            tree = await tools.snapshot(include_iframes=include_iframes)
            header = await asyncio.wait_for(self._header(), self._settings.capture_timeout)
            await self._raise_if_lost()
            return self._with_refusals(f"{header}\n{tree}")

    async def run(self, *, code: str | None = None, actions: Sequence[Mapping[str, Any]] | None = None) -> str:
        """Run page code or a batch of actions on the active page; pass exactly one.

        Returns the text for the agent, before the result ceiling is applied.
        """
        _check_run_arguments(code, actions)  # before the lock and before any launch
        async with self._lock:
            await self._raise_if_lost()
            tools = await self._ensure_launched()
            try:
                value = await tools.run(code=code, actions=actions)
            except RPCError as error:
                await self._raise_if_lost()
                if re.search(r"callback batch timed out", str(error), re.I):
                    raise RunTimeoutError(self._settings.run_timeout) from None
                raise
            await self._raise_if_lost()
            text = page_code_text(value)
            if text is None:
                text = f"{NO_VALUE} {await self._current_url()}"
            return self._with_refusals(text)

    async def console(self, *, level: str = "info", all: bool = False, page: str | None = None) -> str:
        """What a page logged: the active page, or the one `page` names.

        Needs the debugging group; reads the capture buffer and does not launch the browser.
        """
        async with self._lock:
            await self._raise_if_lost()
            if self._capture is None:
                return self._nothing_captured("console", "logged")
            active = await self._active_page_id() if page is None else None
            return console_text.render_report(await self._capture.read(page, active, level=level, all=all))

    async def network(
        self,
        *,
        all: bool = False,
        page: str | None = None,
        failed: bool = False,
        url: str | None = None,
        types: str = "default",
        id: str | None = None,
    ) -> str:
        """The requests of a page, or the headers and body of the one request `id` names.

        Needs the debugging group; reads the capture buffer and does not launch the browser.
        Raises BodyNotHeldError when a text body is gone; its message holds the rest of the detail.
        """
        async with self._lock:
            await self._raise_if_lost()
            if self._capture is None:
                return self._nothing_captured("network", "requested")
            if id is not None:
                request_page, request, late_body = await self._capture.request(id)
                return network_text.render_detail(request, request_page, sanitize_error_message, lazy_body=late_body)
            active = await self._active_page_id() if page is None else None
            report = await self._capture.read_network(
                page, active, all=all, failed=failed, url=url, types=types, redact=sanitize_error_message
            )
            return network_text.render_report(report, sanitize_error_message)

    def _nothing_captured(self, tool: str, verb: str) -> str:
        if self._tools is None and "debugging" in self._settings.groups:
            return f"No browser is open yet, so nothing was {verb}. Open a page with run, then call {tool}."
        raise RuntimeError(f"Capture is off. Add debugging to the groups setting to turn on {tool}.")

    async def close(self) -> None:
        """Close the browser if one was launched. Never raises."""
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        """Close the connection and the browser, and forget both. Never raises."""
        tools, self._tools, self._stagehand = self._tools, None, None
        connection, self._connection, self._capture, self._enforcer = self._connection, None, None, None
        self._closing = True
        try:
            if connection is not None:
                await connection.close()
        finally:
            self._closing = False
        if tools is None:
            return
        try:
            await tools.close()
        except Exception as error:
            _log(f"browser-tools: closing the browser failed: {error}")

    async def _raise_if_lost(self) -> None:
        """Report a lost browser once, and drop it so that the next call launches a fresh one.

        The site limits live on the host connection, so a browser without it must not go on.
        """
        if self._lost is None:
            return
        cause, self._lost = self._lost, None
        await self._teardown()
        raise BrowserLostError(cause)

    def _with_refusals(self, text: str) -> str:
        """The text, with a last paragraph that says what the site limits refused since the last call."""
        notice = refusal_notice(self._enforcer.take_refusals()) if self._enforcer is not None else ""
        return f"{text}\n\n{notice}" if notice else text

    async def _ensure_launched(self) -> StagehandFacadeTools:
        if self._tools is not None:
            return self._tools
        settings = self._settings
        # The middleware's deadlines are module constants.
        _upstream_tools.PAGE_CAPTURE_DEADLINE_S = settings.capture_timeout  # ty: ignore[invalid-assignment]
        _upstream_tools.RUN_BATCH_TIMEOUT_MS = round(settings.run_timeout * 1000)  # ty: ignore[invalid-assignment]
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        browser = await local_browser.launch(
            headless=not settings.headed,
            executable_path=settings.chrome_path,
            user_data_dir=settings.profile_dir,
            downloads_path=settings.download_dir,
            accept_downloads=True,
        )
        connection: CdpConnection | None = None
        try:
            # The host connection comes first, so that capture sees the first navigation.
            if browser.cdp_url is None:
                raise RuntimeError("The browser did not report a debugging address.")
            connection = await CdpConnection.connect(browser.cdp_url)
            limits = SiteLimits.from_entries(
                settings.allow_sites, settings.deny_sites, allow_file_urls=settings.allow_file_urls
            )
            enforcer = SiteEnforcer(connection, limits, self._unsafe) if limits.active else None
            capture = Capture(connection, redact=sanitize_error_message) if "debugging" in settings.groups else None
            listeners = [listener for listener in (enforcer, capture) if listener is not None]
            if listeners:
                targets = Targets(connection)
                for listener in listeners:
                    targets.add_listener(listener)
                await targets.start()
            stagehand = await Stagehand.create(browser=browser)
        except BaseException:
            if connection is not None:
                await connection.close()
            await browser.close()
            raise
        connection.add_close_callback(self._connection_closed)
        self._connection, self._capture, self._enforcer = connection, capture, enforcer
        self._stagehand = stagehand
        self._tools = StagehandFacadeTools(
            stagehand,
            artifact_root=settings.artifact_dir,
            quiet_wait=settings.quiet_wait,
        )
        return self._tools

    def _unsafe(self, cause: str) -> None:
        """The site limits cannot be kept, so the next call reports the browser as lost."""
        _log(f"browser-tools: {cause}.")
        if self._lost is None:
            self._lost = cause

    def _connection_closed(self) -> None:
        if not self._closing:
            _log("browser-tools: the connection to the browser closed.")
            self._lost = "the connection to the browser dropped"

    async def _active_page_id(self) -> str | None:
        assert self._stagehand is not None
        context = self._stagehand.browser.context
        page = await context.active_page()
        if page is None:
            pages = await context.pages()
            page = pages[-1] if pages else None
        return page.page_id if page is not None else None

    async def _current_url(self) -> str:
        assert self._stagehand is not None
        page = await self._stagehand.browser.context.active_page()
        return await page.url() if page is not None else "about:blank"

    async def _header(self) -> str:
        """`Page: <title> | <url>` for the active page, read just after the capture."""
        assert self._stagehand is not None
        context = self._stagehand.browser.context
        page = await context.active_page()
        if page is None:
            pages = await context.pages()
            if not pages:
                raise RuntimeError("The browser has no open page; call snapshot again.")
            page = pages[-1]
        return f"Page: {await page.title()} | {await page.url()}"
