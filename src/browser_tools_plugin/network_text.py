"""The `network` tool's data and text: requests, the body cache, and the list and detail output."""

import collections
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote_plus

from .console_text import BUFFER_LIMIT, ENTRY_LIMIT, PageRef, cut, one_line, other_pages_line, page_label

REQUEST_BUFFER_LIMIT = BUFFER_LIMIT  # requests kept per page
BODY_ITEM_LIMIT = 256 * 1024  # largest body that is cached, in bytes
BODY_CACHE_LIMIT = 50 * 1024 * 1024  # all cached bodies of one browser, in bytes
BODY_SHOW_LIMIT = 10 * 1024  # largest body in a result, in bytes

# Header values that are secrets. The names are compared in lower case.
SECRET_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
# A header or a body field whose name holds one of these words is a secret, in any case.
# It is a substring match, so a few harmless names match too, such as `Sec-WebSocket-Key`.
SECRET_WORDS = ("auth", "token", "key", "secret", "session", "csrf", "xsrf", "signature", "password", "credential")
# Request types that the list hides unless `types` is `all`.
HIDDEN_TYPES = frozenset({"Image", "Font", "Media", "Stylesheet"})

_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/xml",
        "application/xhtml+xml",
        "application/x-www-form-urlencoded",
        "application/graphql",
        "image/svg+xml",
    }
)

Redactor = Callable[[str], str]


class BodyNotHeldError(Exception):
    """A text body that the server no longer has. The message holds the rest of the detail."""


def is_text_like(mime_type: str) -> bool:
    """Whether a body of this content type is text that is worth keeping."""
    mime = mime_type.split(";", 1)[0].strip().lower()
    return mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+json", "+xml"))


@dataclass(slots=True)
class Request:
    """One request of a page, from its start to its end."""

    id: str  # `r12`: one counter for the whole browser
    method: str
    url: str
    resource_type: str
    session: str  # the CDP session that Chrome reported it on; used to read a body late
    cdp_id: str
    start: float  # Chrome's monotonic clock, in seconds
    request_headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None
    has_post_data: bool = False
    status: int | None = None
    status_text: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    failure: str | None = None
    duration: float | None = None  # seconds; None while the request waits for its first answer
    finished: bool = False
    redirected: bool = False  # the response was a redirect, so it has no body
    size: int = 0  # bytes of body data seen
    body: str | None = None  # a cached text body
    body_note: str = ""  # why a finished request holds no body
    unread: bool = True
    released: bool = False  # dropped from its page's buffer
    binary: bool = False  # the body turned out not to be text
    request_headers_full: bool = False  # headers came from the extra-info event
    response_headers_full: bool = False

    @property
    def failed(self) -> bool:
        return self.failure is not None or (self.status is not None and self.status >= 400)

    @property
    def text_like(self) -> bool:
        return not self.binary and is_text_like(self.mime_type)


class BodyCache:
    """Holds text bodies on their requests, within a byte limit. The oldest go first."""

    def __init__(self, limit: int = BODY_CACHE_LIMIT) -> None:
        self._limit = limit
        self._held: collections.OrderedDict[str, Request] = collections.OrderedDict()
        self._bytes = 0

    @property
    def bytes_held(self) -> int:
        return self._bytes

    def add(self, request: Request, body: str, size: int) -> None:
        request.body, request.size = body, size
        self._held[request.id] = request
        self._bytes += size
        while self._bytes > self._limit:
            _, oldest = self._held.popitem(last=False)
            self._bytes -= oldest.size
            oldest.body = None
            oldest.body_note = "it was dropped from the body cache, which is full"

    def discard(self, request: Request) -> None:
        if self._held.pop(request.id, None) is not None:
            self._bytes -= request.size
        request.body = None


def is_secret_name(name: str) -> bool:
    """Whether a header or field of this name holds a secret."""
    lower = name.lower()
    return lower in SECRET_HEADERS or any(word in lower for word in SECRET_WORDS)


def _masked(size: int) -> str:
    return f"[redacted, {size} chars]"


def mask_header(name: str, value: str, redact: Redactor) -> list[str]:
    """The display lines of one header. A secret shows only its length; one line per value."""
    values = value.split("\n")
    if is_secret_name(name):
        return [f"{name}: {_masked(len(v))}" for v in values]
    return [f"{name}: {cut(one_line(redact(v)))}" for v in values]


def _mask_json_value(value: object) -> tuple[object, bool]:
    """The value with every secret field masked, and whether anything changed."""
    if isinstance(value, dict):
        changed = False
        result: dict[str, object] = {}
        for key, item in value.items():
            if is_secret_name(key):
                size = len(item) if isinstance(item, str) else len(json.dumps(item, separators=(",", ":")))
                result[key] = _masked(size)
                changed = True
            else:
                result[key], inner = _mask_json_value(item)
                changed = changed or inner
        return result, changed
    if isinstance(value, list):
        items = [_mask_json_value(item) for item in value]
        return [item for item, _ in items], any(inner for _, inner in items)
    return value, False


# A `"name": value` pair, for JSON that does not parse because it was cut. The value is a
# string, which may be cut too, or the bare text up to the next separator.
_JSON_FIELD = re.compile(r'("(?:[^"\\]|\\.)*"\s*:\s*)("(?:[^"\\]|\\.)*"?|[^\s,}\]]+)')


def _mask_json_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    else:
        masked, changed = _mask_json_value(parsed)
        return json.dumps(masked, separators=(",", ":"), ensure_ascii=False) if changed else text

    def replace(match: re.Match[str]) -> str:
        head, value = match.groups()
        try:
            name = json.loads(head[: head.rindex('"') + 1])
        except ValueError:
            return match.group(0)
        if not is_secret_name(name):
            return match.group(0)
        size = len(value.strip('"')) if value.startswith('"') else len(value)
        return f'{head}"{_masked(size)}"'

    return _JSON_FIELD.sub(replace, text)


def _mask_form_text(text: str) -> str:
    parts = []
    for part in text.split("&"):
        name, equals, value = part.partition("=")
        if equals and is_secret_name(unquote_plus(name)):
            part = f"{name}={_masked(len(unquote_plus(value)))}"
        parts.append(part)
    return "&".join(parts)


def mask_request_body(text: str, content_type: str | None) -> str:
    """A request body with the values of secret fields masked. Only form-encoded and JSON bodies
    have fields. A body that looks like JSON counts as JSON whatever its content type says,
    because `fetch` sends a string body as text/plain."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime == "application/x-www-form-urlencoded":
        return _mask_form_text(text)
    if mime == "application/json" or mime.endswith("+json") or text.lstrip()[:1] in ("{", "["):
        return _mask_json_text(text)
    return text


def _status_text(request: Request) -> str:
    if request.failure is not None:
        return "failed"
    return "pending" if request.status is None else str(request.status)


def _milliseconds(request: Request) -> str:
    return "-" if request.duration is None else f"{round(request.duration * 1000)}ms"


def request_line(request: Request, redact: Redactor) -> str:
    """`[r12] GET 404 132ms https://…`, and the failure text at the end of a failed request."""
    line = f"[{request.id}] {request.method} {_status_text(request)} {_milliseconds(request)} "
    line += cut(one_line(redact(request.url)))
    if request.failure is not None:
        line += f" ({cut(one_line(request.failure), 80)})"
    return line


@dataclass(slots=True)
class NetworkReport:
    page: PageRef
    shown: list[Request]  # newest first
    matching: int  # requests that fit the request: new, or all, and every filter
    held: int  # requests in the buffer
    dropped: int
    all: bool
    failed: bool
    url: str | None
    types: str
    hidden: int  # requests that only the `types` filter kept out
    hidden_failed: int  # of those, the failed ones
    others: list[PageRef]


def render_report(report: NetworkReport, redact: Redactor) -> str:
    """Header lines, then one line per request, newest first."""
    which = "requests" if report.all else "new requests"
    lines = [f"Network of page: {page_label(report.page)}"]
    qualifiers = ""
    if report.failed:
        qualifiers += " that failed"
    if report.url:
        qualifiers += f" with a URL that contains {cut(one_line(report.url), 60)!r}"
    count = f"{len(report.shown)} of {report.matching}" if len(report.shown) < report.matching else f"{report.matching}"
    lines.append(f"{count} {which}{qualifiers}, newest first.")
    if len(report.shown) < report.matching:
        older = report.matching - len(report.shown)
        if report.all:
            lines.append(f"{older} older requests are not shown. Use failed or url to see fewer requests.")
        else:
            lines.append(f"{older} older new requests are not shown. Call network again to read them.")
    if report.dropped:
        lines.append(
            f"{report.dropped} older requests were dropped from this page's buffer, "
            f"which keeps the last {REQUEST_BUFFER_LIMIT}."
        )
    if report.hidden:
        failed = f", {report.hidden_failed} of them failed" if report.hidden_failed else ""
        lines.append(
            f"{report.hidden} image, font, media or stylesheet requests are hidden{failed}. "
            'Pass types: "all" to list them.'
        )
    if report.matching == 0 and not report.all and report.held:
        lines.append(f"Nothing new. The buffer holds {report.held} requests; pass all: true to read them again.")
    if report.others:
        lines.append(other_pages_line(report.others))
    lines.extend(request_line(request, redact) for request in report.shown)
    return "\n".join(lines)


def _cut_body(text: str, redact: Redactor) -> tuple[str, int]:
    """The text to show, with the secrets in it masked, and the full size in bytes.

    The whole text is masked before the cut, so that a secret at the cut is found.
    """
    size = len(text.encode())
    masked = redact(text)
    if size <= BODY_SHOW_LIMIT:
        return masked, size
    return masked.encode()[:BODY_SHOW_LIMIT].decode(errors="ignore"), size


def _body_block(title: str, text: str, redact: Redactor) -> list[str]:
    shown, size = _cut_body(text, redact)
    if size > BODY_SHOW_LIMIT:
        title += f" ({size} bytes; the first {BODY_SHOW_LIMIT} are shown, {size - BODY_SHOW_LIMIT} are cut)"
    else:
        title += f" ({size} bytes)"
    return [f"{title}:", shown]


def _header_block(title: str, headers: dict[str, str], redact: Redactor) -> list[str]:
    if not headers:
        return [f"{title}: none held."]
    lines = [f"{title}:"]
    for name, value in headers.items():
        lines.extend(f"  {line}" for line in mask_header(name, value, redact))
    return lines


def render_detail(request: Request, page: PageRef, redact: Redactor, *, lazy_body: str | None = None) -> str:
    """The headers and bodies of one request.

    `lazy_body` is a body that was read from Chrome just now because the cache did not hold it.
    Raises BodyNotHeldError, with this text as its message, when a text body is gone.
    """
    status = _status_text(request)
    if request.status_text and request.failure is None and request.status is not None:
        status += f" {request.status_text}"
    if request.failure is not None:
        status += f": {request.failure}"
    lines = [
        f"Request {request.id} on page: {page_label(page)}",
        f"{request.method} {cut(one_line(redact(request.url)), 1000)}",
        f"Status: {status}; time: {_milliseconds(request)}; type: {request.resource_type or 'unknown'}",
        *_header_block("Request headers", request.request_headers, redact),
    ]
    if request.post_data:
        content_type = next((v for n, v in request.request_headers.items() if n.lower() == "content-type"), None)
        lines += _body_block("Request body", request.post_data, lambda t: redact(mask_request_body(t, content_type)))
    elif request.has_post_data:
        lines.append("Request body: not held. Chrome did not send it with the request.")
    lines += _header_block("Response headers", request.response_headers, redact)
    body = request.body if request.body is not None else lazy_body
    gone: str | None = None
    if request.failure is not None:
        lines.append("Response body: none, because the request failed.")
    elif request.status is None:
        lines.append("Response body: none yet, because the request is still pending. Call network again later.")
    elif request.redirected:
        lines.append("Response body: none, because the response was a redirect.")
    elif body is not None:
        lines += _body_block("Response body", body, redact)
    elif not request.text_like:
        kind = request.mime_type.split(";", 1)[0].strip() or "unknown"
        lines.append(f"Response body: not shown, because it is not text. Type: {kind}; size: {request.size} bytes.")
    else:
        reason = request.body_note or "the request did not finish before the page went away"
        gone = (
            f"The body of {request.id} is not held: {reason}. "
            "Chrome drops a body when its page navigates. So reproduce the request and call network again."
        )
        lines.append(f"Response body: not held ({reason}).")
    text = "\n".join(lines)
    if gone is not None:
        raise BodyNotHeldError(f"{gone}\n\n{text}")
    return text


def read_matching(
    requests: collections.deque[Request],
    *,
    all: bool,
    failed: bool,
    url: str | None,
    types: str,
    redact: Redactor,
) -> tuple[list[Request], int, int, int]:
    """The requests that fit, newest first and at most ENTRY_LIMIT; then the counts of requests
    that only the `types` filter hid, and how many of those failed. Requests shown count as read.

    The `url` filter looks at the masked URL, the one a line shows, so a filter cannot be used
    to find out a masked value."""
    needle = url.casefold() if url else None
    matching: list[Request] = []
    hidden = hidden_failed = 0
    for request in requests:
        if not (all or request.unread):
            continue
        if failed and not request.failed:
            continue
        if needle is not None and needle not in redact(request.url).casefold():
            continue
        if types != "all" and request.resource_type in HIDDEN_TYPES:
            hidden += 1
            hidden_failed += request.failed
            continue
        matching.append(request)
    shown = matching[::-1][:ENTRY_LIMIT]
    for request in shown:
        request.unread = False
    return shown, len(matching), hidden, hidden_failed
