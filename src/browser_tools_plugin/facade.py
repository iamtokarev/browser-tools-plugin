"""The facade seam: the only module that touches `stagehand` and `stagehand-middleware`.

Every adjustment to upstream behaviour lives here, so that upgrading either pinned
package, or replacing the middleware with direct SDK calls, changes this file only.
"""

import asyncio
import base64
import contextlib
import math
import re
import shutil
import sys
import tempfile
from collections.abc import AsyncGenerator, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from stagehand import Stagehand, local_browser
from stagehand.browser import _find_chrome_path
from stagehand.rpc_client import RPCError
from stagehand_middleware.facade import tools as _upstream_tools
from stagehand_middleware.facade.bundle import facade_tools
from stagehand_middleware.facade.tools import StagehandFacadeTools, sanitize_error_message

from . import console_text, network_text
from .capture import Capture
from .cdp import CdpConnection, CdpError
from .downloads import Downloads, downloads_notice, seed_download_directory
from .lifecycle import ChromeProcess, Pidfile, ProfileLock, ask_to_close, find_main_process
from .limits import SiteEnforcer, refusal_notice
from .results import NO_VALUE, page_code_text
from .screenshots import (
    MAX_SIDE_PX,
    Attempt,
    ImageType,
    Screenshot,
    ScreenshotTooLargeError,
    fits_bytes,
    fits_side,
    image_size,
    ladder,
)
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


THROWAWAY_NOTICE = (
    "Another browser-tools server holds the dedicated profile, so this session uses a throwaway profile. "
    "Logins and cookies from the dedicated profile are not available here. "
    "The throwaway profile is deleted when this session ends."
)

_LOST_EXITED = "the browser process exited"
_LOST_DROPPED = "the connection to the browser dropped"
_EXIT_CHECKS = 20  # times 0.05 s: how long a dropped connection waits for the process to end
_EXIT_CHECK_INTERVAL = 0.05
_LOST_NO_TAB = "the last tab was closed"


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class BrowserSession:
    """One local browser per server process, launched on the first call that needs it.

    One lock serialises every call, including the launch.

    The session also owns the profile. The dedicated profile is held with a lock file for the whole
    session, and a pidfile names its Chrome. When another server holds the profile, or the
    `throwaway_profile` setting is on, the session uses a throwaway profile that it deletes at the end.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._tools: StagehandFacadeTools | None = None
        self._stagehand: Stagehand | None = None
        self._connection: CdpConnection | None = None
        self._capture: Capture | None = None
        self._enforcer: SiteEnforcer | None = None
        self._downloads: Downloads | None = None
        self._lost: str | None = None  # why the browser counts as lost, until the next call reports it
        self._closing = False
        self._profile: Path | None = None  # the profile of this session, chosen at the first launch
        self._profile_lock: ProfileLock | None = None
        self._pidfile: Pidfile | None = None
        self._throwaway: Path | None = None
        self._chrome: ChromeProcess | None = None
        self._pending_notices: list[str] = []  # notices for the first text result

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
        async with self._call():
            tools = await self._ensure_launched()
            tree = await tools.snapshot(include_iframes=include_iframes)
            header = await asyncio.wait_for(self._header(), self._settings.capture_timeout)
            await self._raise_if_lost()
            return await self._with_notices(f"{header}\n{tree}")

    async def screenshot(
        self, *, full_page: bool = False, type: ImageType = "jpeg", quality: float | None = None
    ) -> Screenshot:
        """The active page as an image that fits the client limits, after the fallback ladder.

        Raises ScreenshotTooLargeError when no rung fits. A page that the site limits blocked
        shows its notice page, and that is a page like any other.
        """
        async with self._call():
            await self._ensure_launched()
            page = await self._active_page()
            try:
                shot = await asyncio.wait_for(
                    self._climb(page, full_page=full_page, type=type, quality=quality),
                    self._settings.capture_timeout,
                )
            except TimeoutError:
                raise RuntimeError(
                    f"page.screenshot received no response within {round(self._settings.capture_timeout * 1000)}ms. "
                    "Call snapshot to see the page, then try again."
                ) from None
            await self._raise_if_lost()
            return shot

    async def _climb(self, page: Any, *, full_page: bool, type: ImageType, quality: float | None) -> Screenshot:
        """Try each rung of the ladder until one fits.

        A rung is first captured at the pixel size of the browser. Sometimes that image is too wide,
        for example on a 2x screen. A lower quality does not help then. So the same rung is
        captured again, scaled down to the side limit, and the next rungs stay scaled.
        A full page that is too large is not scaled. The next rung is the viewport,
        because a scaled page of several screens is too small to read.
        """
        scaled = False
        for attempt in ladder(full_page=full_page, type=type, quality=quality):
            shot = await (self._shoot_scaled(page, attempt) if scaled else self._shoot(page, attempt))
            if not scaled and not attempt.full_page and not fits_side(shot.width, shot.height):
                shot = await self._shoot_scaled(page, attempt)
                scaled = True
            if fits_side(shot.width, shot.height) and fits_bytes(shot.data):
                return shot
        raise ScreenshotTooLargeError

    @staticmethod
    async def _shoot(page: Any, attempt: Attempt) -> Screenshot:
        data = await page.screenshot(type=attempt.type, full_page=attempt.full_page, quality=attempt.quality)
        width, height = image_size(data)
        return Screenshot(data, attempt.type, attempt.quality, attempt.full_page, width, height, scaled=False)

    async def _shoot_scaled(self, page: Any, attempt: Attempt) -> Screenshot:
        """The attempt again over the host connection, with a clip scale that keeps it to the side limit.

        The SDK's screenshot has no clip scale, and its own `scale` option does not shrink an image.
        The page ID of the SDK is the target ID of Chrome.
        """
        assert self._connection is not None
        connection = self._connection
        attached = await connection.send("Target.attachToTarget", {"targetId": page.page_id, "flatten": True})
        session = attached["sessionId"]
        try:
            metrics = await connection.send("Page.getLayoutMetrics", session=session)
            size = metrics["cssContentSize"] if attempt.full_page else metrics["cssVisualViewport"]
            width = size["width"] if attempt.full_page else size["clientWidth"]
            height = size["height"] if attempt.full_page else size["clientHeight"]
            # Rounded down, so that rounding the pixels up cannot pass the limit.
            scale = min(1.0, math.floor(MAX_SIDE_PX / max(width, height, 1) * 1000) / 1000)
            params: dict[str, Any] = {
                "format": attempt.type,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": scale},
                "captureBeyondViewport": attempt.full_page,
            }
            if attempt.quality is not None:
                params["quality"] = attempt.quality
            result = await connection.send("Page.captureScreenshot", params, session=session)
        finally:
            with contextlib.suppress(CdpError):
                await connection.send("Target.detachFromTarget", {"sessionId": session})
        data = base64.b64decode(result["data"])
        pixels = image_size(data)
        return Screenshot(data, attempt.type, attempt.quality, attempt.full_page, *pixels, scaled=True)

    async def run(self, *, code: str | None = None, actions: Sequence[Mapping[str, Any]] | None = None) -> str:
        """Run page code or a batch of actions on the active page; pass exactly one.

        Returns the text for the agent, before the result ceiling is applied.
        """
        _check_run_arguments(code, actions)  # before the lock and before any launch
        async with self._call():
            tools = await self._ensure_launched()
            try:
                value = await tools.run(code=code, actions=actions)
            except RPCError as error:
                await self._check_alive()
                await self._raise_if_lost()
                if re.search(r"callback batch timed out", str(error), re.I):
                    raise RunTimeoutError(self._settings.run_timeout) from None
                raise
            await self._raise_if_lost()
            text = page_code_text(value)
            if text is None:
                text = f"{NO_VALUE} {await self._current_url()}"
            return await self._with_notices(text)

    async def console(self, *, level: str = "info", all: bool = False, page: str | None = None) -> str:
        """What a page logged: the active page, or the one `page` names.

        Needs the debugging group; reads the capture buffer and does not launch the browser.
        """
        async with self._call():
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
        async with self._call():
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
        """Stop the browser if one was launched, and give up the profile. Never raises."""
        async with self._lock:
            await self._teardown(final=True)

    @contextlib.asynccontextmanager
    async def _call(self) -> AsyncGenerator[None]:
        """One call, under the one lock.

        A browser that is gone is reported before the work starts. An error from the work is replaced
        by the lost-browser error when the cause is that the browser went away.
        """
        async with self._lock:
            await self._check_alive()
            await self._raise_if_lost()
            try:
                yield
            except Exception:
                await self._check_alive()
                await self._raise_if_lost()
                raise
            await self._raise_if_lost()

    async def _check_alive(self) -> None:
        """Mark the browser as lost when its process is gone, its connection is closed or it has no tab."""
        if self._tools is None:
            return
        if self._lost is None and self._chrome is not None and not self._chrome.running:
            self._lost = _LOST_EXITED
        if self._lost is None and self._connection is not None:
            try:
                reply = await self._connection.send("Target.getTargets")
            except CdpError:
                self._lost = _LOST_DROPPED
            else:
                if not any(target.get("type") == "page" for target in reply.get("targetInfos", [])):
                    self._lost = _LOST_NO_TAB
                return
        if self._lost == _LOST_DROPPED and await self._chrome_exits_soon():
            self._lost = _LOST_EXITED

    async def _chrome_exits_soon(self) -> bool:
        """True when our Chrome is gone, or goes within a short wait.

        A process that is killed closes its sockets before the system shows it as ended. So the connection
        can drop a short time before the process is gone, and the cause is then the exit, not the connection.
        """
        if self._chrome is None:
            return False
        for _ in range(_EXIT_CHECKS):
            if not self._chrome.running:
                return True
            await asyncio.sleep(_EXIT_CHECK_INTERVAL)
        return not self._chrome.running

    async def _teardown(self, *, final: bool = False) -> None:
        """Close the connection and stop the browser, and forget both. Never raises.

        Chrome is asked to close itself first, which writes its profile, cookies included. A Chrome that
        stays gets SIGTERM, and after 5 s its process group gets SIGKILL. Stagehand's own
        close runs afterwards, and its error is swallowed: its browser is already gone. With `final`,
        the profile lock goes too, and a throwaway profile is deleted.
        """
        tools, self._tools, self._stagehand = self._tools, None, None
        connection, self._connection, self._capture, self._enforcer = self._connection, None, None, None
        chrome, self._chrome = self._chrome, None
        self._downloads = None
        self._closing = True
        try:
            if connection is not None and chrome is not None:
                # The orderly exit writes the cookies that a signal would lose.
                await ask_to_close(connection, chrome)
            if connection is not None:
                await connection.close()
        finally:
            self._closing = False
        stopped = chrome is None
        if chrome is not None:
            try:
                stopped = await chrome.stop()
            except Exception as error:
                _log(f"browser-tools: stopping the browser failed: {error!r}")
            if not stopped:
                _log("browser-tools: the browser did not stop.")
        if tools is not None:
            try:
                await tools.close()
            except Exception as error:
                if chrome is None:
                    _log(f"browser-tools: closing the browser failed: {error}")
        if stopped and self._pidfile is not None:
            self._pidfile.remove()
        if final:
            if self._profile_lock is not None:
                self._profile_lock.release()
            if self._throwaway is not None:
                shutil.rmtree(self._throwaway, ignore_errors=True)

    async def _raise_if_lost(self) -> None:
        """Report a lost browser once, and drop it so that the next call launches a fresh one.

        The site limits live on the host connection, so a browser without it must not go on.
        """
        if self._lost is None:
            return
        cause, self._lost = self._lost, None
        await self._teardown()
        raise BrowserLostError(cause)

    async def _with_notices(self, text: str) -> str:
        """The text, with last paragraphs that say what the site limits refused and what was downloaded."""
        notices, self._pending_notices = self._pending_notices, []
        if self._enforcer is not None:
            notices.append(refusal_notice(self._enforcer.take_refusals()))
        if self._downloads is not None:
            finished, running = await self._downloads.settle()
            notices.append(downloads_notice(finished, running))
        return "\n\n".join([text, *(notice for notice in notices if notice)])

    async def _ensure_launched(self) -> StagehandFacadeTools:
        if self._tools is not None:
            await self._hold_download_directory()
            return self._tools
        settings = self._settings
        # The middleware's deadlines are module constants.
        _upstream_tools.PAGE_CAPTURE_DEADLINE_S = settings.capture_timeout  # ty: ignore[invalid-assignment]
        _upstream_tools.RUN_BATCH_TIMEOUT_MS = round(settings.run_timeout * 1000)  # ty: ignore[invalid-assignment]
        profile = await self._claim_profile()
        profile.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        # Before Chrome starts, so that a download lands in the directory even when no CDP command is in force.
        # The throwaway profile gets it too.
        seed_download_directory(profile, settings.download_dir)
        browser = await local_browser.launch(
            headless=not settings.headed,
            executable_path=settings.chrome_path,
            user_data_dir=profile,
        )
        connection: CdpConnection | None = None
        try:
            # The host connection comes first, so that capture sees the first navigation.
            if browser.cdp_url is None:
                raise RuntimeError("The browser did not report a debugging address.")
            connection = await CdpConnection.connect(browser.cdp_url)
            chrome = await find_main_process(connection, profile)
            downloads = Downloads(connection, settings.download_dir)
            await downloads.start()  # the SDK's own `downloads_path` overwrites files of the same name
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
        self._chrome = chrome
        if chrome is None:
            _log("browser-tools: the main process of the browser was not found. Stagehand closes it at shutdown.")
        elif self._pidfile is not None:
            port = urlsplit(browser.cdp_url).port
            try:
                self._pidfile.write(chrome, port)
            except OSError as error:
                _log(f"browser-tools: the pidfile could not be written: {error}")
        self._connection, self._capture, self._enforcer = connection, capture, enforcer
        self._downloads = downloads
        self._stagehand = stagehand
        self._tools = StagehandFacadeTools(
            stagehand,
            artifact_root=settings.artifact_dir,
            quiet_wait=settings.quiet_wait,
        )
        return self._tools

    async def _claim_profile(self) -> Path:
        """The profile of this session. The first launch chooses it, and a later launch uses the same one.

        The dedicated profile is taken with a lock file, and a Chrome that an earlier server left on it
        is stopped. A profile that another server holds, or `throwaway_profile`, gives a throwaway profile.
        """
        if self._profile is not None:
            return self._profile
        settings = self._settings
        if not settings.throwaway_profile:
            lock = ProfileLock(settings.profile_dir.with_name(f"{settings.profile}.lock"))
            if lock.acquire():
                self._profile_lock = lock
                self._pidfile = Pidfile(settings.profile_dir.with_name(f"{settings.profile}.pid"))
                self._profile = settings.profile_dir
                await self._pidfile.kill_leftover(settings.profile_dir)
                return self._profile
            self._pending_notices.append(THROWAWAY_NOTICE)
        self._throwaway = Path(tempfile.mkdtemp(prefix="browser-tools-throwaway-"))
        self._profile = self._throwaway
        return self._profile

    async def _hold_download_directory(self) -> None:
        """Set the download behaviour again before a call, because page code can have changed it.

        When Chrome refuses, the directory is no longer held, so the browser counts as lost.
        """
        if self._downloads is None:
            return
        try:
            await self._downloads.assert_directory()
        except CdpError as error:
            self._unsafe(f"the download directory could not be set: {error}")
            await self._raise_if_lost()

    def _unsafe(self, cause: str) -> None:
        """The site limits cannot be kept, so the next call reports the browser as lost."""
        _log(f"browser-tools: {cause}.")
        if self._lost is None:
            self._lost = cause

    def _connection_closed(self) -> None:
        if not self._closing:
            _log("browser-tools: the connection to the browser closed.")
            self._lost = _LOST_DROPPED

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

    async def _active_page(self) -> Any:
        """The active page, or the newest one when none is active."""
        assert self._stagehand is not None
        context = self._stagehand.browser.context
        page = await context.active_page()
        if page is None:
            pages = await context.pages()
            if not pages:
                raise RuntimeError("The browser has no open page; open one with run, then try again.")
            page = pages[-1]
        return page

    async def _header(self) -> str:
        """`Page: <title> | <url>` for the active page, read just after the capture."""
        page = await self._active_page()
        return f"Page: {await page.title()} | {await page.url()}"
