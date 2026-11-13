"""Result shaping: pure functions, no browser."""

from browser_tools_plugin.results import (
    NO_VALUE,
    cap_run_result,
    cap_snapshot,
    page_code_text,
)


def test_a_string_value_is_returned_as_is():
    assert page_code_text('hello "there"') == 'hello "there"'


def test_other_values_are_compact_json():
    assert page_code_text({"a": 1, "b": [1, 2], "c": "é"}) == '{"a":1,"b":[1,2],"c":"é"}'
    assert page_code_text(7) == "7"
    assert page_code_text(False) == "false"
    assert page_code_text([]) == "[]"


def test_no_value_is_none():
    assert page_code_text(None) is None
    assert NO_VALUE == "Done. No value returned."


def test_run_result_at_the_ceiling_is_unchanged():
    assert cap_run_result("x" * 10, 10) == "x" * 10


def test_run_result_above_the_ceiling_is_the_head_and_the_notice():
    text = cap_run_result("a" * 25, 10)
    head, _, notice = text.partition("\n\n")
    assert head == "a" * 10
    assert "result was 25 characters; the code did run; return less, for example pick fields, slice or count" in notice
    assert "10" in notice


def test_snapshot_above_the_ceiling_cuts_at_a_line_and_counts_elements():
    lines = [f"[0-{n}] button: item {n}" for n in range(10)]
    text = "\n".join(lines)
    ceiling = len("\n".join(lines[:4])) + 5  # inside line 5
    result = cap_snapshot(text, ceiling)
    head, _, notice = result.partition("\n\n")
    assert head == "\n".join(lines[:4])
    cut = len(text) - len(head)
    assert f"{cut} characters" in notice
    assert "6 elements" in notice
    assert "run code" in notice
    assert "do not call snapshot again" in notice.lower()


def test_snapshot_at_the_ceiling_is_unchanged():
    assert cap_snapshot("[0-1] a\n[0-2] b", 100) == "[0-1] a\n[0-2] b"
