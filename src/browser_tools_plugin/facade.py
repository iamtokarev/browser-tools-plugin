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
from stagehand.rpc_client import RPCError
from stagehand_middleware.facade import tools as _upstream_tools
from stagehand_middleware.facade.bundle import facade_tools
from stagehand_middleware.facade.tools import StagehandFacadeTools, sanitize_error_message

from .results import NO_VALUE, page_code_text
from .settings import Settings

__all__ = ["BrowserSession", "RunTimeoutError", "sanitize_error_message", "upstream_tool"]


def upstream_tool(name: str) -> dict[str, Any]:
    """Upstream's tool definition from the "explicit snapshot actions" set."""
    for tool in facade_tools("explicitSnapshotActions"):
        if tool["name"] == name:
            return deepcopy(tool)
    raise KeyError(name)


class RunTimeoutError(Exception):
    """A run went past its deadline and the middleware stopped it."""

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"Run exceeded {seconds:g} s and was stopped. Some of it may have taken effect; "
            "call snapshot to see the page before retrying. Split long work into smaller runs."
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

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def launched(self) -> bool:
        return self._tools is not None

    async def snapshot(self, *, include_iframes: bool = True) -> str:
        """The active page's tree, led by one `Page: <title> | <url>` line."""
        async with self._lock:
            tools = await self._ensure_launched()
            tree = await tools.snapshot(include_iframes=include_iframes)
            header = await asyncio.wait_for(self._header(), self._settings.capture_timeout)
            return f"{header}\n{tree}"

    async def run(self, *, code: str | None = None, actions: Sequence[Mapping[str, Any]] | None = None) -> str:
        """Run page code or a batch of actions on the active page; pass exactly one.

        Returns the text for the agent, before the result ceiling is applied.
        """
        _check_run_arguments(code, actions)  # before the lock and before any launch
        async with self._lock:
            tools = await self._ensure_launched()
            try:
                value = await tools.run(code=code, actions=actions)
            except RPCError as error:
                if re.search(r"callback batch timed out", str(error), re.I):
                    raise RunTimeoutError(self._settings.run_timeout) from None
                raise
            text = page_code_text(value)
            if text is None:
                return f"{NO_VALUE} {await self._current_url()}"
            return text

    async def close(self) -> None:
        """Close the browser if one was launched. Never raises."""
        async with self._lock:
            tools, self._tools, self._stagehand = self._tools, None, None
            if tools is None:
                return
            try:
                await tools.close()
            except Exception as error:
                _log(f"browser-tools: closing the browser failed: {error}")

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
        try:
            stagehand = await Stagehand.create(browser=browser)
        except BaseException:
            await browser.close()
            raise
        self._stagehand = stagehand
        self._tools = StagehandFacadeTools(
            stagehand,
            artifact_root=settings.artifact_dir,
            quiet_wait=settings.quiet_wait,
        )
        return self._tools

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
