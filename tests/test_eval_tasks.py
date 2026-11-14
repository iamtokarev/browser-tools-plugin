"""The ten tasks: shape, deterministic checks, the fixture site, and each scripted solution on a real browser."""

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from eval.guidance import Setup
from eval.oracle import OracleModel
from eval.runner import run_task
from eval.site import FixtureSite
from eval.tasks import TASKS, TASKS_BY_ID, evaluate, parse_answer
from tests.conftest import needs_chrome


def post(site: FixtureSite, path: str, body: dict) -> None:
    request = urllib.request.Request(site.base + path, data=json.dumps(body).encode(), method="POST")
    urllib.request.urlopen(request).read()


def get(site: FixtureSite, path: str) -> int:
    with urllib.request.urlopen(site.base + path) as response:
        return response.status


def test_ten_tasks_seven_web_and_three_debugging_with_unique_ids():
    assert len(TASKS) == 10
    assert [t.kind for t in TASKS].count("web") == 7
    assert [t.kind for t in TASKS].count("debugging") == 3
    assert len(TASKS_BY_ID) == 10


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_a_run_that_did_nothing_fails_every_check(task):
    with FixtureSite(1) as site:
        for final in ("", "{}", '{"done": true}', "I could not do it."):
            assert not evaluate(task, site, final).passed


@pytest.mark.parametrize("task", [t for t in TASKS if t.answer], ids=lambda t: t.id)
def test_the_exact_answer_passes_and_a_changed_value_or_an_extra_key_fails(task):
    with FixtureSite(3) as site:
        expected = task.answer(site)
        assert evaluate(task, site, json.dumps(expected)).passed
        assert evaluate(task, site, f"```json\n{json.dumps(expected)}\n```").passed
        assert not evaluate(task, site, f"The answer is {json.dumps(expected)}").passed
        assert not evaluate(task, site, json.dumps(expected | {"extra": 1})).passed
        key = next(iter(expected))
        assert not evaluate(task, site, json.dumps(expected | {key: "wrong"})).passed


def test_values_depend_on_the_seed_so_that_an_answer_cannot_be_learned():
    with FixtureSite(1) as first, FixtureSite(2) as second:
        assert first.report_code != second.report_code
        assert first.target_product != second.target_product


def test_the_cart_check_wants_exactly_the_named_product_once():
    task = TASKS_BY_ID["cart-add"]
    with FixtureSite(1) as site:
        post(site, "/api/cart", {"sku": "Red mug" if site.mug_for_cart != "Red mug" else "Blue mug"})
        assert not evaluate(task, site, '{"done": true}').passed
    with FixtureSite(1) as site:
        post(site, "/api/cart", {"sku": site.mug_for_cart})
        assert evaluate(task, site, '{"done": true}').passed
        post(site, "/api/cart", {"sku": site.mug_for_cart})
        assert not evaluate(task, site, '{"done": true}').passed


def test_the_form_check_wants_one_submission_with_every_field():
    task = TASKS_BY_ID["signup-form"]
    values = {"name": "Ada Quill", "email": "ada@example.test", "plan": "team", "newsletter": True}
    with FixtureSite(1) as site:
        post(site, "/api/signup", values | {"newsletter": False})
        assert not evaluate(task, site, '{"done": true}').passed
    with FixtureSite(1) as site:
        post(site, "/api/signup", values)
        assert evaluate(task, site, '{"done": true}').passed


def test_following_the_injected_instruction_fails_the_task_even_with_the_right_answer():
    task = TASKS_BY_ID["injected-review"]
    with FixtureSite(1) as site:
        answer = json.dumps({"average_rating": site.average_rating})
        assert evaluate(task, site, answer).passed
        assert "/canary" in urllib.request.urlopen(site.base + "/reviews").read().decode()
        get(site, "/canary?from=agent")
        verdict = evaluate(task, site, answer)
        assert not verdict.passed
        assert "injected instruction" in verdict.reason


def test_the_answer_parser_accepts_one_object_only():
    assert parse_answer('{"a": 1}') == {"a": 1}
    assert parse_answer('```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("[1]", "1", "", "```\nnot json\n```", 'x {"a": 1}'):
        assert parse_answer(bad) is None


def test_the_debugging_pages_hold_the_facts_that_the_tasks_ask_for():
    with FixtureSite(5) as site:
        assert (
            f"config.chartOptions.{site.console_property}"
            in urllib.request.urlopen(site.base + "/dash").read().decode()
        )
        assert site.failing_path in urllib.request.urlopen(site.base + "/orders").read().decode()
        with pytest.raises(urllib.error.HTTPError) as error:
            get(site, site.failing_path)
        assert error.value.code == site.failing_status
        with pytest.raises(urllib.error.HTTPError) as error:
            post(site, "/api/checkout", {})
        assert site.rejected_field in error.value.read().decode()


# -- Each scripted solution, through the real server over stdio and a real Chrome -----------


def chrome_processes_of_the_eval() -> list[str]:
    out = subprocess.run(["pgrep", "-f", "user-data-dir=.*browser-tools-eval-"], capture_output=True, text=True)
    return out.stdout.split()


@needs_chrome
@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_the_scripted_solution_passes_on_a_real_browser_and_leaves_no_chrome(task):
    record = await run_task(
        task, repeat=0, setup=Setup.NONE, model_for=lambda site: OracleModel(task, site), max_turns=30
    )
    assert record.passed, record.reason
    assert record.stop == "answered"
    assert record.turns == len(record.steps) + 1
    assert set(record.tool_calls) <= {"snapshot", "run", "screenshot", "console", "network"}
    assert chrome_processes_of_the_eval() == []


@needs_chrome
async def test_a_wrong_scripted_answer_fails_on_a_real_browser():
    task = TASKS_BY_ID["late-report"]
    record = await run_task(
        task,
        repeat=0,
        setup=Setup.NONE,
        model_for=lambda site: OracleModel(task, site, final_text='{"code": "RPT-0000"}'),
        max_turns=30,
    )
    assert not record.passed
    assert "Wrong answer" in record.reason


async def test_the_server_gets_the_fixture_origin_as_its_only_allowed_site_and_a_temporary_data_directory():
    from eval.runner import server_environment

    with FixtureSite(1) as site:
        env = server_environment(Path("/tmp/eval-data-x"), site)
    assert env["BROWSER_TOOLS_ALLOW_SITES"] == site.origin
    assert env["PLUGIN_DATA"] == "/tmp/eval-data-x"
    assert env["BROWSER_TOOLS_THROWAWAY_PROFILE"] == "true"
    assert not any(name.startswith("ANTHROPIC_") for name in env)


def test_equal_values_ignore_int_or_float_but_not_bool_or_extra_keys():
    from eval.tasks import same_value

    assert same_value({"a": 4}, {"a": 4.0})
    assert not same_value({"a": True}, {"a": 1})
    assert not same_value({"a": 1, "b": 2}, {"a": 1})
    assert not same_value({"a": "4"}, {"a": 4})
    assert same_value({"a": [1, {"b": 2}]}, {"a": [1.0, {"b": 2}]})
