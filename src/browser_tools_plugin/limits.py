"""The site-limit enforcer: refuses requests that the limits forbid, before they leave the browser.

It listens on the host CDP connection, on every page and iframe from creation, so page code
cannot switch it off. Every request pauses in `Fetch`, and the enforcer continues, fails or
answers it. A refused page load gets a small page that says why, so the agent does not retry.
Known gaps: WebSockets are not seen by `Fetch`, and the first request of a tab that was created
with a URL leaves before the tab is closed. See the spec, section 4.2.
"""

import base64
import contextlib
import html
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .cdp import CdpConnection, CdpError
from .sites import SiteLimits
from .targets import WORKER_TYPES, TargetListener

BLOCKED_TITLE = "Blocked by site limits"
BLOCKED_STATUS = 403
REFUSAL_LIMIT = 50  # refusals that are kept between two tool calls
NOTICE_LIMIT = 5  # refusals that one notice lists


@dataclass(frozen=True, slots=True)
class Refusal:
    url: str  # as the agent may read it: no query
    reason: str
    closed_tab: bool = False


def blocked_page(url: str, reason: str) -> bytes:
    text = f"{BLOCKED_TITLE}: {url} was not opened, because {reason}."
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{BLOCKED_TITLE}</title></head><body>"
        f"<h1>{html.escape(text)}</h1>"
        "<p>The user set these limits and you cannot change them. "
        "Do not look for a way around them. Tell the user if you need this site.</p>"
        "</body></html>"
    ).encode()


def refusal_notice(refusals: list[Refusal]) -> str:
    """One paragraph for the end of a tool result that says what was refused."""
    if not refusals:
        return ""
    parts = []
    for refusal in refusals[:NOTICE_LIMIT]:
        what = f"a tab on {refusal.url} was closed" if refusal.closed_tab else refusal.url
        parts.append(f"{what} ({refusal.reason})")
    more = len(refusals) - NOTICE_LIMIT
    tail = f"; and {more} more" if more > 0 else ""
    return (
        f"Refused by site limits: {'; '.join(parts)}{tail}. "
        "No request was made to a refused site. The user set these limits; do not try to get around them."
    )


class SiteEnforcer(TargetListener):
    def __init__(self, connection: CdpConnection, limits: SiteLimits, on_unsafe: Callable[[str], None]) -> None:
        """`on_unsafe(cause)` is called when the browser cannot be kept under the limits and must be dropped."""
        self._cdp = connection
        self._limits = limits
        self._on_unsafe = on_unsafe
        self._refusals: list[Refusal] = []

    def take_refusals(self) -> list[Refusal]:
        """The refusals since the last call. They are not returned twice."""
        taken, self._refusals = self._refusals, []
        return taken

    def _record(self, refusal: Refusal) -> None:
        if refusal not in self._refusals and len(self._refusals) < REFUSAL_LIMIT:
            self._refusals.append(refusal)

    def on_attach(self, session: str, info: dict[str, Any], target_id: str) -> list[Awaitable[Any]]:
        return [self._enable(session, info, target_id)]

    async def _enable(self, session: str, info: dict[str, Any], target_id: str) -> None:
        """Start the pause of every request. A tab where this fails would be open to every site, so it is closed."""
        try:
            await self._cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=session)
        except CdpError as error:
            print(
                f"browser-tools: site limits could not start on a {info['type']}: {error}", file=sys.stderr, flush=True
            )
            if info["type"] in WORKER_TYPES:
                raise  # `Targets` keeps the worker paused and calls `on_attach_failed`
            await self._close(target_id)

    async def on_attach_failed(self, session: str, info: dict[str, Any], target_id: str, error: BaseException) -> None:
        """A worker that cannot be covered is never resumed. An ordinary one is closed. The browser's own
        extension runs the page code, so closing it would break the browser: the browser counts as lost."""
        if str(info.get("url", "")).startswith("chrome-extension://"):
            self._on_unsafe("the site limits could not be applied to the browser's own extension")
            return
        await self._close(target_id)

    def on_event(self, target_id: str, session: str, method: str, params: dict[str, Any]) -> Awaitable[None] | None:
        if method == "Fetch.requestPaused":
            return self._on_paused(session, params)
        return None

    async def _on_paused(self, session: str, params: dict[str, Any]) -> None:
        request_id = params["requestId"]
        url = params["request"]["url"]
        try:
            reason = self._limits.check(url)
        except Exception as error:  # fail closed: a request that cannot be judged is refused
            print(f"browser-tools: a site-limit check failed: {error!r}", file=sys.stderr, flush=True)
            reason = "the site limits could not check it"
        with contextlib.suppress(CdpError):  # the page went away, or the connection closed
            if reason is None:
                await self._cdp.send("Fetch.continueRequest", {"requestId": request_id}, session=session)
                return
            shown = SiteLimits.describe_url(url)
            self._record(Refusal(shown, reason))
            if params.get("resourceType") == "Document":
                await self._cdp.send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": request_id,
                        "responseCode": BLOCKED_STATUS,
                        "responsePhrase": BLOCKED_TITLE,
                        "responseHeaders": [
                            {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                            # An opaque origin, so that the page cannot read the cookies of the refused site.
                            {"name": "Content-Security-Policy", "value": "sandbox"},
                        ],
                        "body": base64.b64encode(blocked_page(shown, reason)).decode(),
                    },
                    session=session,
                )
            else:
                await self._cdp.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}, session=session
                )

    def on_target_created(self, info: dict[str, Any]) -> Awaitable[None] | None:
        """A tab that was created on a refused URL is closed. A popup is not: its page load is refused."""
        if info.get("type") != "page" or info.get("openerId"):
            return None
        url = info.get("url", "")
        reason = self._limits.check(url) if url else None
        if reason is None:
            return None
        self._record(Refusal(SiteLimits.describe_url(url), reason, closed_tab=True))
        return self._close(info["targetId"])

    async def _close(self, target_id: str) -> None:
        with contextlib.suppress(CdpError):
            await self._cdp.send("Target.closeTarget", {"targetId": target_id})
