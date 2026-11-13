"""The capture buffer: what each page logged, kept from browser launch onward.

The buffer listens on the host CDP connection. It attaches to every page and iframe from the
moment Chrome creates it, so the first navigation and the first events of a popup are not lost.
This version keeps console entries. Request capture will use the same attachment.
"""

import asyncio
import collections
import contextlib
import time
from collections.abc import Awaitable
from typing import Any

from .cdp import CdpConnection, CdpError
from .console_text import (
    BUFFER_LIMIT,
    ENTRY_LIMIT,
    LEVELS,
    ConsoleReport,
    Entry,
    PageRef,
    format_location,
    render_arguments,
)

_CONSOLE_LEVELS = {"debug": "debug", "error": "error", "assert": "error", "warning": "warning"}
_LOG_LEVELS = {"verbose": "debug", "info": "info", "warning": "warning", "error": "error"}
# Log entries that repeat what the console API call already reported.
_LOG_SOURCES_SKIPPED = {"console-api"}


class UnknownPageError(LookupError):
    """The `page` argument names no open page, or more than one."""


class PageBuffer:
    """Everything captured for one tab: its console entries, with its title and URL."""

    def __init__(self, target_id: str, limit: int = BUFFER_LIMIT) -> None:
        self.target_id = target_id
        self.title = ""
        self.url = ""
        self.dropped = 0
        self._limit = limit
        self._entries: collections.deque[Entry] = collections.deque()
        self._logged = 0  # entries that are not navigation markers
        self._next_seq = 0

    @property
    def ref(self) -> PageRef:
        return PageRef(self.title, self.url)

    @property
    def held(self) -> int:
        return self._logged

    def add(self, *, at: float, level: str, text: str, location: str | None = None, marker: bool = False) -> None:
        """Append one entry. Over the limit, the oldest logged entries go first. A marker goes
        with the entries around it; only a page that navigates without logging can pile up
        markers, so the oldest marker goes when there are more markers than the limit."""
        self._entries.append(Entry(self._next_seq, at, level, text, location, marker))
        self._next_seq += 1
        if marker:
            if len(self._entries) - self._logged > self._limit:
                del self._entries[next(i for i, entry in enumerate(self._entries) if entry.marker)]
            return
        self._logged += 1
        while self._logged > self._limit:
            if not self._entries.popleft().marker:
                self._logged -= 1
                self.dropped += 1

    def read(self, *, level: str, all: bool) -> ConsoleReport:
        """Entries at `level` or above, newest first and at most ENTRY_LIMIT. Entries shown
        count as read. Without `all`, only unread entries are chosen."""
        floor = LEVELS.index(level)
        # A marker counts as an info line: a higher level hides it.
        matching = [
            entry
            for entry in self._entries
            if (all or entry.unread) and LEVELS.index("info" if entry.marker else entry.level) >= floor
        ]
        shown = matching[::-1][:ENTRY_LIMIT]
        for entry in shown:
            entry.unread = False
        return ConsoleReport(
            page=self.ref,
            shown=shown,
            matching=len(matching),
            held=len(self._entries),
            dropped=self.dropped,
            level=level,
            all=all,
            others=[],
        )


class Capture:
    """Attaches to every page and iframe and fills one PageBuffer per tab."""

    def __init__(self, connection: CdpConnection, *, limit: int = BUFFER_LIMIT) -> None:
        self._cdp = connection
        self._limit = limit
        self._pages: dict[str, PageBuffer] = {}
        self._owner: dict[str, str] = {}  # session id -> target id of the tab it belongs to
        self._claimed: set[str] = set()  # targets that were attached, or are being attached
        self._started = False

    @property
    def pages(self) -> list[PageBuffer]:
        return list(self._pages.values())

    def page(self, target_id: str) -> PageBuffer | None:
        return self._pages.get(target_id)

    async def start(self) -> None:
        """Attach to the tabs that exist, and to each new tab and iframe from its creation."""
        if self._started:
            return
        self._started = True
        self._cdp.add_handler(self._on_event)
        await self._cdp.send("Target.setDiscoverTargets", {"discover": True})
        await self._cdp.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
                "filter": [{"type": "page"}, {"type": "iframe"}, {"exclude": True}],
            },
        )
        targets = (await self._cdp.send("Target.getTargets"))["targetInfos"]
        for info in targets:
            if info["type"] == "page" and info["targetId"] not in self._claimed:
                self._claimed.add(info["targetId"])
                reply = await self._cdp.send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
                await self._setup(reply["sessionId"], info, None, waiting=False)

    def find(self, query: str | None, active_id: str | None) -> PageBuffer:
        """The active page, or the one page whose title or URL holds `query`."""
        if query is None or not query.strip():
            if active_id is not None and active_id in self._pages:
                return self._pages[active_id]
            raise UnknownPageError("The active page has no captured console. Call snapshot, then try again.")
        needle = query.strip().casefold()
        found = [p for p in self._pages.values() if needle in p.title.casefold() or needle in p.url.casefold()]
        if len(found) == 1:
            return found[0]
        open_pages = "; ".join(f"{p.title} | {p.url}" for p in self._pages.values()) or "none"
        reason = "matches no open page" if not found else f"matches {len(found)} open pages"
        raise UnknownPageError(f"The page {query!r} {reason}. Open pages: {open_pages}.")

    async def refresh(self) -> None:
        """Take each tab's current title and URL from Chrome, and forget tabs that are gone.

        Chrome's change events can come before the title is known, so a read asks again.
        """
        try:
            targets = (await self._cdp.send("Target.getTargets"))["targetInfos"]
        except CdpError:
            return
        alive = {info["targetId"]: info for info in targets if info["type"] == "page"}
        for target_id in [t for t in self._pages if t not in alive]:
            del self._pages[target_id]
        for info in alive.values():
            self._on_target_info(info)

    async def read(self, query: str | None, active_id: str | None, *, level: str, all: bool) -> ConsoleReport:
        await self.refresh()
        buffer = self.find(query, active_id)
        report = buffer.read(level=level, all=all)
        report.others = [p.ref for p in self._pages.values() if p is not buffer]
        return report

    # --- Attaching -------------------------------------------------------------------------

    def _on_event(self, message: dict[str, Any]) -> Awaitable[None] | None:
        method, params, session = message.get("method"), message.get("params", {}), message.get("sessionId")
        if method == "Target.attachedToTarget":
            return self._on_attached(params, session)
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            self._on_target_info(params["targetInfo"])
        elif method == "Target.targetDestroyed":
            self._pages.pop(params["targetId"], None)
        elif method == "Target.detachedFromTarget":
            self._owner.pop(params.get("sessionId"), None)
        elif session in self._owner:
            self._on_page_event(self._pages.get(self._owner[session]), method, params)
        return None

    def _on_target_info(self, info: dict[str, Any]) -> None:
        if info["type"] != "page":
            return
        buffer = self._pages.get(info["targetId"])
        if buffer is not None:
            buffer.title, buffer.url = info.get("title", ""), info.get("url", "")

    def _on_attached(self, params: dict[str, Any], parent_session: str | None) -> Awaitable[None] | None:
        info = params["targetInfo"]
        waiting = params.get("waitingForDebugger", False)
        if info["type"] not in ("page", "iframe"):
            # The filter keeps these away; if one arrives, never leave it paused.
            return self._resume(params["sessionId"]) if waiting else None
        if info["targetId"] in self._claimed:
            return None  # start() attached this tab itself
        self._claimed.add(info["targetId"])
        return self._setup(params["sessionId"], info, parent_session, waiting=waiting)

    async def _resume(self, session: str) -> None:
        with contextlib.suppress(CdpError):
            await self._cdp.send("Runtime.runIfWaitingForDebugger", session=session)

    async def _setup(self, session: str, info: dict[str, Any], parent_session: str | None, *, waiting: bool) -> None:
        target_id = info["targetId"] if info["type"] == "page" else self._owner.get(parent_session or "")
        if target_id is None:
            if waiting:
                await self._resume(session)
            return
        self._owner[session] = target_id
        if info["type"] == "page":
            buffer = self._pages.setdefault(target_id, PageBuffer(target_id, self._limit))
            buffer.title, buffer.url = info.get("title", ""), info.get("url", "")
        # Send every command without waiting for each answer, and resume last. A popup opened by
        # `target=_blank` answers no command until it resumes, so waiting for the first answer
        # would stall; resuming first would lose its first events.
        send = self._cdp.send
        commands = [
            send("Runtime.enable", session=session),
            send("Log.enable", session=session),
            send("Page.enable", session=session),
            send(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                    "filter": [{"type": "iframe"}, {"exclude": True}],
                },
                session=session,
            ),
        ]
        if waiting:
            commands.append(send("Runtime.runIfWaitingForDebugger", session=session))
        await asyncio.gather(*commands, return_exceptions=True)

    # --- Events ----------------------------------------------------------------------------

    def _on_page_event(self, buffer: PageBuffer | None, method: str | None, params: dict[str, Any]) -> None:
        if buffer is None:
            return
        if method == "Runtime.consoleAPICalled":
            self._on_console_call(buffer, params)
        elif method == "Runtime.exceptionThrown":
            self._on_exception(buffer, params)
        elif method == "Log.entryAdded":
            self._on_log_entry(buffer, params["entry"])
        elif method == "Page.frameNavigated" and not params["frame"].get("parentId"):
            buffer.url = params["frame"]["url"]
            buffer.add(at=time.time(), level="info", text=f"navigated to {buffer.url}", marker=True)

    @staticmethod
    def _on_console_call(buffer: PageBuffer, params: dict[str, Any]) -> None:
        kind = params["type"]
        if kind in ("clear", "endGroup"):
            return
        text = render_arguments(params.get("args", []))
        if kind == "assert":
            text = f"Assertion failed: {text}" if text else "Assertion failed"
        frames = params.get("stackTrace", {}).get("callFrames", [])
        location = None
        if frames:
            top = frames[0]
            location = format_location(top.get("url"), top.get("lineNumber"), top.get("columnNumber"))
        buffer.add(
            at=params["timestamp"] / 1000,
            level=_CONSOLE_LEVELS.get(kind, "info"),
            text=text,
            location=location,
        )

    @staticmethod
    def _on_exception(buffer: PageBuffer, params: dict[str, Any]) -> None:
        details = params["exceptionDetails"]
        exception = details.get("exception") or {}
        detail = (exception.get("description") or str(exception.get("value", ""))).split("\n", 1)[0]
        head = details.get("text", "Uncaught")
        text = head if not detail or detail in head else f"{head} {detail}"
        url = details.get("url") or (details.get("stackTrace", {}).get("callFrames") or [{}])[0].get("url")
        buffer.add(
            at=params["timestamp"] / 1000,
            level="error",
            text=text,
            location=format_location(url, details.get("lineNumber"), details.get("columnNumber")),
        )

    @staticmethod
    def _on_log_entry(buffer: PageBuffer, entry: dict[str, Any]) -> None:
        if entry.get("source") in _LOG_SOURCES_SKIPPED:
            return
        buffer.add(
            at=entry["timestamp"] / 1000,
            level=_LOG_LEVELS.get(entry["level"], "info"),
            text=entry["text"],
            location=format_location(entry.get("url"), entry.get("lineNumber"), None),
        )
