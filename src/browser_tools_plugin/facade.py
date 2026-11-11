"""The facade seam: the only module that touches `stagehand` and `stagehand-middleware`.

Every adjustment to upstream behaviour lives here, so that upgrading either pinned
package, or replacing the middleware with direct SDK calls, changes this file only.
"""

import asyncio
import sys
from copy import deepcopy
from typing import Any

from stagehand import Stagehand, local_browser
from stagehand_middleware.facade import tools as _upstream_tools
from stagehand_middleware.facade.bundle import facade_tools
from stagehand_middleware.facade.tools import StagehandFacadeTools, sanitize_error_message

from .settings import Settings

__all__ = ["BrowserSession", "sanitize_error_message", "upstream_tool"]


def upstream_tool(name: str) -> dict[str, Any]:
    """Upstream's tool definition from the "explicit snapshot actions" set."""
    for tool in facade_tools("explicitSnapshotActions"):
        if tool["name"] == name:
            return deepcopy(tool)
    raise KeyError(name)


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
    def launched(self) -> bool:
        return self._tools is not None

    async def snapshot(self, *, include_iframes: bool = True) -> str:
        """The active page's tree, led by one `Page: <title> | <url>` line."""
        async with self._lock:
            tools = await self._ensure_launched()
            tree = await tools.snapshot(include_iframes=include_iframes)
            header = await asyncio.wait_for(self._header(), self._settings.capture_timeout)
            return f"{header}\n{tree}"

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
        _upstream_tools.PAGE_CAPTURE_DEADLINE_S = settings.capture_timeout
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
