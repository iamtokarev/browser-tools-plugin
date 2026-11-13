"""The capture buffer: what each page logged, kept from browser launch onward.

The buffer listens on the host CDP connection. It attaches to every page and iframe from the
moment Chrome creates it, so the first navigation and the first events of a popup are not lost.
It keeps console entries and network requests. Text bodies are cached as each request finishes,
because Chrome drops a body once its page navigates.
"""

import collections
import itertools
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
from .network_text import (
    BODY_CACHE_LIMIT,
    BODY_ITEM_LIMIT,
    REQUEST_BUFFER_LIMIT,
    BodyCache,
    NetworkReport,
    Redactor,
    Request,
    read_matching,
)
from .targets import TargetListener

_CONSOLE_LEVELS = {"debug": "debug", "error": "error", "assert": "error", "warning": "warning"}
_LOG_LEVELS = {"verbose": "debug", "info": "info", "warning": "warning", "error": "error"}
# Log entries that repeat what the console API call already reported.
_LOG_SOURCES_SKIPPED = {"console-api"}


class UnknownPageError(LookupError):
    """The `page` argument names no open page, or more than one."""


class UnknownRequestError(LookupError):
    """The `id` argument names no request that a page still holds."""


class PageBuffer:
    """Everything captured for one tab: its console entries and requests, with its title and URL."""

    def __init__(
        self,
        target_id: str,
        limit: int = BUFFER_LIMIT,
        request_limit: int = REQUEST_BUFFER_LIMIT,
        *,
        redact: Redactor = str,
    ) -> None:
        self.target_id = target_id
        self._redact = redact
        self.title = ""
        self.url = ""
        self.dropped = 0
        self.requests_dropped = 0
        self._limit = limit
        self._request_limit = request_limit
        self._requests: collections.deque[Request] = collections.deque()
        self._entries: collections.deque[Entry] = collections.deque()
        self._logged = 0  # entries that are not navigation markers
        self._next_seq = 0

    @property
    def ref(self) -> PageRef:
        """The title and URL as the agent may read them: the secrets in them are masked."""
        return PageRef(self._redact(self.title), self._redact(self.url))

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

    @property
    def requests(self) -> collections.deque[Request]:
        return self._requests

    def add_request(self, request: Request) -> list[Request]:
        """Append one request. Over the limit, the oldest go first; they are returned."""
        self._requests.append(request)
        removed: list[Request] = []
        while len(self._requests) > self._request_limit:
            old = self._requests.popleft()
            old.released = True
            self.requests_dropped += 1
            removed.append(old)
        return removed

    def read_requests(self, *, all: bool, failed: bool, url: str | None, types: str, redact: Redactor) -> NetworkReport:
        """Requests that fit, newest first and at most ENTRY_LIMIT. Requests shown count as read.
        Without `all`, only unread requests are chosen."""
        shown, matching, hidden, hidden_failed = read_matching(
            self._requests, all=all, failed=failed, url=url, types=types, redact=redact
        )
        return NetworkReport(
            page=self.ref,
            shown=shown,
            matching=matching,
            held=len(self._requests),
            dropped=self.requests_dropped,
            all=all,
            failed=failed,
            url=url,
            types=types,
            hidden=hidden,
            hidden_failed=hidden_failed,
            others=[],
        )

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


class Capture(TargetListener):
    """Listens to every page and iframe and fills one PageBuffer per tab."""

    def __init__(
        self,
        connection: CdpConnection,
        *,
        limit: int = BUFFER_LIMIT,
        request_limit: int = REQUEST_BUFFER_LIMIT,
        body_item_limit: int = BODY_ITEM_LIMIT,
        body_cache_limit: int = BODY_CACHE_LIMIT,
        redact: Redactor,
    ) -> None:
        self._cdp = connection
        self._redact = redact
        self._limit = limit
        self._request_limit = request_limit
        self._body_item_limit = body_item_limit
        self._cache = BodyCache(body_cache_limit)
        self._request_ids = itertools.count(1)
        self._live: dict[tuple[str, str], Request] = {}  # (session, CDP request id) -> its latest request
        self._early_headers: dict[tuple[str, str], dict[str, str]] = {}  # extra info that came first
        self._redirected: dict[tuple[str, str], Request] = {}  # the last redirect hop of a request
        self._pages: dict[str, PageBuffer] = {}

    @property
    def pages(self) -> list[PageBuffer]:
        return list(self._pages.values())

    def page(self, target_id: str) -> PageBuffer | None:
        return self._pages.get(target_id)

    def find(self, query: str | None, active_id: str | None) -> PageBuffer:
        """The active page, or the one page whose title or URL holds `query`."""
        if query is None or not query.strip():
            if active_id is not None and active_id in self._pages:
                return self._pages[active_id]
            raise UnknownPageError("The active page has no capture buffer. Call snapshot, then try again.")
        needle = query.strip().casefold()
        # The match looks at what a header shows, so that a query cannot be used to find out a masked value.
        found = []
        for page in self._pages.values():
            ref = page.ref
            if needle in ref.title.casefold() or needle in ref.url.casefold():
                found.append(page)
        if len(found) == 1:
            return found[0]
        open_pages = "; ".join(f"{p.ref.title} | {p.ref.url}" for p in self._pages.values()) or "none"
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
            self._forget(target_id)
        for info in alive.values():
            self.on_target_info(info)

    async def read(self, query: str | None, active_id: str | None, *, level: str, all: bool) -> ConsoleReport:
        await self.refresh()
        buffer = self.find(query, active_id)
        report = buffer.read(level=level, all=all)
        report.others = [p.ref for p in self._pages.values() if p is not buffer]
        return report

    async def read_network(
        self,
        query: str | None,
        active_id: str | None,
        *,
        all: bool,
        failed: bool,
        url: str | None,
        types: str,
        redact: Redactor,
    ) -> NetworkReport:
        await self.refresh()
        buffer = self.find(query, active_id)
        report = buffer.read_requests(all=all, failed=failed, url=url, types=types, redact=redact)
        report.others = [p.ref for p in self._pages.values() if p is not buffer]
        return report

    async def request(self, request_id: str) -> tuple[PageRef, Request, str | None]:
        """The request with this id, its page, and its body when the cache does not hold it but
        Chrome still does. Looks at every page."""
        await self.refresh()
        for buffer in self._pages.values():
            for request in buffer.requests:
                if request.id == request_id:
                    return buffer.ref, request, await self._read_late(request)
        raise UnknownRequestError(
            f"No page holds a request {request_id}. Each page keeps its last {self._request_limit} requests, "
            "and a closed page keeps none. Call network to list the requests that are held."
        )

    async def _read_late(self, request: Request) -> str | None:
        """Ask Chrome for a text body that is not cached. It works while the page has not navigated."""
        if request.body is not None or not request.finished or request.redirected or request.failure is not None:
            return None
        if request.status is None or request.binary or not request.text_like:
            return None
        try:
            reply = await self._cdp.send(
                "Network.getResponseBody", {"requestId": request.cdp_id}, session=request.session
            )
        except CdpError:
            return None
        if reply.get("base64Encoded"):
            request.binary = True
            return None
        return reply["body"]

    def _forget(self, target_id: str) -> None:
        """Drop a closed page, and the bodies and lookups of its requests."""
        buffer = self._pages.pop(target_id, None)
        if buffer is not None:
            for request in buffer.requests:
                self._release(request)

    def _release(self, request: Request) -> None:
        request.released = True
        self._cache.discard(request)
        key = (request.session, request.cdp_id)
        if self._redirected.get(key) is request:
            del self._redirected[key]
        if self._live.get((request.session, request.cdp_id)) is request:
            del self._live[(request.session, request.cdp_id)]

    # --- Attaching -------------------------------------------------------------------------

    def on_target_info(self, info: dict[str, Any]) -> None:
        if info["type"] != "page":
            return
        buffer = self._pages.get(info["targetId"])
        if buffer is not None:
            buffer.title, buffer.url = info.get("title", ""), info.get("url", "")

    def on_target_destroyed(self, target_id: str) -> None:
        self._forget(target_id)

    def on_attach(self, session: str, info: dict[str, Any], target_id: str) -> list[Awaitable[Any]]:
        if info["type"] not in ("page", "iframe"):
            return []
        if info["type"] == "page":
            buffer = self._pages.setdefault(
                target_id, PageBuffer(target_id, self._limit, self._request_limit, redact=self._redact)
            )
            buffer.title, buffer.url = info.get("title", ""), info.get("url", "")
        send = self._cdp.send
        return [
            send("Runtime.enable", session=session),
            send("Log.enable", session=session),
            send("Network.enable", session=session),
            send("Page.enable", session=session),
        ]

    def on_event(self, target_id: str, session: str, method: str, params: dict[str, Any]) -> Awaitable[None] | None:
        return self._on_page_event(self._pages.get(target_id), session, method, params)

    # --- Events ----------------------------------------------------------------------------

    def _on_page_event(
        self, buffer: PageBuffer | None, session: str, method: str, params: dict[str, Any]
    ) -> Awaitable[None] | None:
        if buffer is None:
            return None
        if method.startswith("Network."):
            return self._on_network_event(buffer, session, method, params)
        if method == "Runtime.consoleAPICalled":
            self._on_console_call(buffer, params)
        elif method == "Runtime.exceptionThrown":
            self._on_exception(buffer, params)
        elif method == "Log.entryAdded":
            self._on_log_entry(buffer, params["entry"])
        elif method == "Page.frameNavigated" and not params["frame"].get("parentId"):
            buffer.url = params["frame"]["url"]
            buffer.add(at=time.time(), level="info", text=f"navigated to {buffer.ref.url}", marker=True)
        return None

    # --- Requests --------------------------------------------------------------------------

    def _on_network_event(
        self, buffer: PageBuffer, session: str, method: str, params: dict[str, Any]
    ) -> Awaitable[None] | None:
        key = (session, params.get("requestId", ""))
        request = self._live.get(key)
        if method == "Network.requestWillBeSent":
            self._on_request_start(buffer, key, params)
        elif method == "Network.requestWillBeSentExtraInfo":
            headers = _real_headers(params.get("headers", {}))
            if request is not None and not request.request_headers_full:
                request.request_headers, request.request_headers_full = headers, True
            else:
                self._early_headers[key] = headers
                while len(self._early_headers) > 200:  # extra info that no start event claims
                    del self._early_headers[next(iter(self._early_headers))]
        elif request is None:
            return None
        elif method == "Network.responseReceived":
            _apply_response(request, params["response"])
            request.resource_type = params.get("type") or request.resource_type
            request.duration = params["timestamp"] - request.start
        elif method == "Network.responseReceivedExtraInfo":
            hop = self._redirected.get(key)
            if 300 <= params.get("statusCode", 0) < 400 and hop is not None and not hop.response_headers_full:
                # The headers of a redirect can come after the start event of the next hop.
                hop.response_headers, hop.response_headers_full = _real_headers(params.get("headers", {})), True
                return None
            request.response_headers, request.response_headers_full = _real_headers(params.get("headers", {})), True
        elif method == "Network.dataReceived":
            request.size += params.get("dataLength", 0)
        elif method == "Network.loadingFinished":
            request.finished = True
            request.duration = params["timestamp"] - request.start
            return self._cache_body(request)
        elif method == "Network.loadingFailed":
            request.finished = True
            request.duration = params["timestamp"] - request.start
            reason = params.get("errorText") or "the request failed"
            if params.get("blockedReason"):
                reason += f" (blocked: {params['blockedReason']})"
            request.failure = reason
        return None

    def _on_request_start(self, buffer: PageBuffer, key: tuple[str, str], params: dict[str, Any]) -> None:
        previous = self._live.get(key)
        redirect = params.get("redirectResponse")
        if previous is not None and redirect:
            _apply_response(previous, redirect)
            previous.redirected = True
            previous.finished = True
            previous.duration = params["timestamp"] - previous.start
            self._redirected[key] = previous
        sent = params["request"]
        request = Request(
            id=f"r{next(self._request_ids)}",
            method=sent.get("method", "GET"),
            url=sent["url"],
            resource_type=params.get("type", ""),
            session=key[0],
            cdp_id=key[1],
            start=params["timestamp"],
            request_headers=dict(sent.get("headers", {})),
            post_data=sent.get("postData"),
            has_post_data=sent.get("hasPostData", False),
        )
        early = self._early_headers.pop(key, None)
        if early is not None:
            request.request_headers, request.request_headers_full = early, True
        self._live[key] = request
        for old in buffer.add_request(request):
            self._release(old)

    async def _cache_body(self, request: Request) -> None:
        """Keep the body of a text response now, because Chrome drops it when the page navigates."""
        if request.released or request.status is None or request.failure is not None:
            return
        if not request.text_like:
            return
        if request.size > self._body_item_limit:
            request.body_note = (
                f"it is larger than {self._body_item_limit // 1024} KB, which is the most the cache holds"
            )
            return
        try:
            reply = await self._cdp.send(
                "Network.getResponseBody", {"requestId": request.cdp_id}, session=request.session
            )
        except CdpError:
            request.body_note = "Chrome no longer had it when the request finished"
            return
        if reply.get("base64Encoded"):
            request.binary = True
            return
        body = reply["body"]
        size = len(body.encode())
        if request.released:
            return
        if size > self._body_item_limit:
            request.size = size
            request.body_note = (
                f"it is larger than {self._body_item_limit // 1024} KB, which is the most the cache holds"
            )
            return
        self._cache.add(request, body, size)

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


def _real_headers(headers: dict[str, str]) -> dict[str, str]:
    """HTTP/2 pseudo-headers such as `:authority` are not headers the page sets."""
    return {name: value for name, value in headers.items() if not name.startswith(":")}


def _apply_response(request: Request, response: dict[str, Any]) -> None:
    request.status = int(response["status"])
    request.status_text = response.get("statusText", "")
    request.mime_type = response.get("mimeType", "")
    if not request.response_headers_full:
        request.response_headers = dict(response.get("headers", {}))
