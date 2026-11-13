"""Text for the `console` tool: console arguments as one line, and the result's lines and header."""

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

LEVELS = ("debug", "info", "warning", "error")  # lowest first

BUFFER_LIMIT = 1000  # console entries kept per page
LINE_TEXT_LIMIT = 300
ENTRY_LIMIT = 100  # entries in one result
OTHER_PAGES_LIMIT = 8

_FORMAT_SPECIFIER = re.compile(r"%[sdifoOc%]")


def cut(text: str, limit: int = LINE_TEXT_LIMIT) -> str:
    """The text, or its first `limit` characters and `…(+N chars)`."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def one_line(text: str) -> str:
    """Line breaks made visible, so that one entry stays on one line."""
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def _number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def _preview_value(prop: dict[str, Any]) -> str:
    value = prop.get("value", "")
    if prop.get("type") == "string":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_preview(preview: dict[str, Any], description: str) -> str:
    items = [_preview_value(p) for p in preview.get("properties", [])]
    subtype = preview.get("subtype")
    if subtype in ("array", "typedarray"):
        body = ", ".join(items + (["…"] if preview.get("overflow") else []))
        return f"{description} [{body}]" if subtype == "typedarray" else f"[{body}]"
    named = [f"{p['name']}: {_preview_value(p)}" for p in preview.get("properties", [])]
    for entry in preview.get("entries", []):
        key = entry.get("key")
        value = _preview_value(entry["value"])
        named.append(f"{_preview_value(key)} => {value}" if key else value)
    body = ", ".join(named + (["…"] if preview.get("overflow") else []))
    prefix = "" if description == "Object" else f"{description} "
    return f"{prefix}{{{body}}}"


def render_argument(arg: dict[str, Any]) -> str:
    """One console argument, as the DevTools console would show it in a line."""
    if "value" in arg:
        value = arg["value"]
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return _number(value)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)
    if "unserializableValue" in arg:
        return str(arg["unserializableValue"])
    kind, subtype = arg.get("type"), arg.get("subtype")
    description = arg.get("description", "")
    if kind == "undefined":
        return "undefined"
    if subtype == "null":
        return "null"
    if kind == "function" or subtype == "error":
        return _first_line(description)
    if kind == "object" and (preview := arg.get("preview")):
        return _render_preview(preview, description or "Object")
    return _first_line(description) or str(kind or "")


def render_arguments(arguments: Sequence[dict[str, Any]]) -> str:
    """All arguments of one console call, with `%s`-style substitution in a leading string."""
    if not arguments:
        return ""
    rest = list(arguments)
    first = rest.pop(0)
    head = render_argument(first)
    if first.get("type") == "string" and _FORMAT_SPECIFIER.search(head):

        def substitute(match: re.Match[str]) -> str:
            token = match.group(0)
            if token == "%%":
                return "%"
            if not rest:
                return token
            argument = rest.pop(0)
            if token == "%c":
                return ""
            return render_argument(argument)

        head = _FORMAT_SPECIFIER.sub(substitute, head)
    return " ".join([head, *(render_argument(a) for a in rest)])


def format_location(url: str | None, line: int | None, column: int | None) -> str | None:
    """`url:line:column`, with line and column counted from 1 as DevTools shows them."""
    if not url:
        return None
    text = cut(url)
    if line is not None and line >= 0:
        text += f":{line + 1}"
        if column is not None and column >= 0:
            text += f":{column + 1}"
    return text


@dataclass(slots=True)
class Entry:
    """One line of a page's console history."""

    seq: int
    time: float  # seconds since the epoch
    level: str  # one of LEVELS
    text: str
    location: str | None = None
    marker: bool = False  # a navigation, not something the page logged
    unread: bool = True


def entry_line(entry: Entry) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(entry.time)) + f".{int(entry.time * 1000) % 1000:03d}"
    if entry.marker:
        return f"{clock} -- {cut(one_line(entry.text))} --"
    line = f"{clock} {entry.level} {cut(one_line(entry.text))}"
    if entry.location:
        line += f" ({entry.location})"
    return line


@dataclass(slots=True)
class PageRef:
    title: str
    url: str


def page_label(page: PageRef) -> str:
    return f"{cut(one_line(page.title), 80)} | {cut(one_line(page.url), 120)}"


@dataclass(slots=True)
class ConsoleReport:
    page: PageRef
    shown: list[Entry]  # newest first
    matching: int  # entries that fit the request: new, or all, at the level or above
    held: int  # entries in the buffer
    dropped: int
    level: str
    all: bool
    others: list[PageRef]


def render_report(report: ConsoleReport) -> str:
    """Header lines, then one line per entry, newest first."""
    which = "entries" if report.all else "new entries"
    lines = [f"Console of page: {page_label(report.page)}"]
    count = f"{len(report.shown)} of {report.matching}" if len(report.shown) < report.matching else f"{report.matching}"
    lines.append(f"{count} {which} at level {report.level} or above, newest first.")
    if len(report.shown) < report.matching:
        older = report.matching - len(report.shown)
        if report.all:
            lines.append(f"{older} older entries are not shown. Raise level to see fewer entries.")
        else:
            lines.append(f"{older} older new entries are not shown. Call console again to read them.")
    if report.dropped:
        lines.append(
            f"{report.dropped} older entries were dropped from this page's buffer, which keeps the last {BUFFER_LIMIT}."
        )
    if report.matching == 0 and not report.all and report.held:
        lines.append(f"Nothing new. The buffer holds {report.held} entries; pass all: true to read them again.")
    if report.others:
        shown = report.others[:OTHER_PAGES_LIMIT]
        text = "; ".join(page_label(p) for p in shown)
        more = len(report.others) - len(shown)
        lines.append(f"Other pages (name one in page): {text}" + (f"; and {more} more" if more else ""))
    lines.extend(entry_line(entry) for entry in report.shown)
    return "\n".join(lines)
