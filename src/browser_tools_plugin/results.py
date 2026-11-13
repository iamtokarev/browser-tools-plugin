"""Shape tool results for small contexts: page-code values and the result ceiling."""

import json
import re
from typing import Any

NO_VALUE = "Done. No value returned."

_ELEMENT_ID = re.compile(r"\[\d+-\d+\]")


def page_code_text(value: Any) -> str | None:
    """A string as is, anything else as compact JSON, and None when there is no value."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def cap_run_result(text: str, ceiling: int) -> str:
    """Above the ceiling: the head, then a notice. This is a normal result, not an error."""
    if len(text) <= ceiling:
        return text
    return (
        f"{text[:ceiling]}\n\n[Cut: result was {len(text)} characters; the code did run; "
        f"return less, for example pick fields, slice or count. "
        f"Only the first {ceiling} characters are shown.]"
    )


def cap_snapshot(text: str, ceiling: int) -> str:
    """Above the ceiling: whole lines of the head, then a notice with what was cut."""
    if len(text) <= ceiling:
        return text
    head = text[:ceiling]
    if text[ceiling] != "\n" and "\n" in head:
        head = head[: head.rindex("\n")]
    cut = text[len(head) :]
    elements = len(_ELEMENT_ID.findall(cut))
    return (
        f"{head}\n\n[Cut: {len(cut)} characters and {elements} elements are missing from the end "
        "of this snapshot. Do not call snapshot again, because it returns the same tree. "
        "Find the rest with run code, for example by reading only the elements you need.]"
    )


def cap_report(text: str, ceiling: int, advice: str, *, listing: bool) -> str:
    """Above the ceiling: whole lines of the head, then a notice with what was cut.

    The entries that were cut still count as read, so the notice says how to narrow the next call.
    """
    if len(text) <= ceiling:
        return text
    head = text[:ceiling]
    if text[ceiling] != "\n" and "\n" in head:
        head = head[: head.rindex("\n")]
    cut = text[len(head) :]
    lines = len(cut.strip("\n").splitlines())
    read = ", and the entries in them count as read" if listing else ""
    return (
        f"{head}\n\n[Cut: {len(cut)} characters in {lines} lines are missing from the end of this result{read}. "
        f"{advice}.]"
    )
