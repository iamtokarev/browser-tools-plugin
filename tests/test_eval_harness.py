"""The guidance setups, the result files, the compare script and the start of `pytest eval`."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from browser_tools_plugin.server import INSTRUCTIONS, create_server
from browser_tools_plugin.settings import Settings
from eval.compare import compare
from eval.compare import main as compare_main
from eval.guidance import BASE_PROMPT, Setup, SkillMissing, build_system_prompt, read_skill
from eval.results import RunRecord, load_results, summarize, write_results

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: use-browser\ndescription: Drive a browser.\n---\n# The loop\n\nSnapshot, act, snapshot.\n"
    )
    return path


def test_each_setup_builds_its_own_system_prompt(skill: Path):
    instructions = "SERVER INSTRUCTIONS"
    none = build_system_prompt(Setup.NONE, instructions, skill)
    only = build_system_prompt(Setup.INSTRUCTIONS, instructions, skill)
    full = build_system_prompt(Setup.SKILL, instructions, skill)
    assert none == BASE_PROMPT
    assert only == f"{BASE_PROMPT}\n\n{instructions}"
    assert full == f"{only}\n\n# The loop\n\nSnapshot, act, snapshot."
    assert "name: use-browser" not in full  # the front matter is not part of the text


def test_the_skill_setup_names_the_missing_file_and_the_others_do_not_need_it(tmp_path: Path):
    missing = tmp_path / "none" / "SKILL.md"
    with pytest.raises(SkillMissing, match="does not exist"):
        build_system_prompt(Setup.SKILL, "I", missing)
    with pytest.raises(SkillMissing):
        read_skill(missing)
    assert build_system_prompt(Setup.NONE, "I", missing) == BASE_PROMPT
    assert "I" in build_system_prompt(Setup.INSTRUCTIONS, "I", missing)


async def test_the_instructions_string_is_the_one_that_the_server_sends(tmp_path: Path):
    async with Client(create_server(Settings(data_dir=tmp_path))) as client:
        assert client.instructions is not None
        assert client.instructions.startswith(INSTRUCTIONS)


def record(task: str, passed: bool, turns: int, tokens: int, seconds: float = 10.0) -> RunRecord:
    return RunRecord(task, "web", 0, passed, "ok", "answered", turns, {"run": turns - 1}, tokens, 0, 0, 0, seconds)


def test_result_files_round_trip_and_summarize_per_task(tmp_path: Path):
    path = tmp_path / "out" / "r.json"
    write_results(path, {"setup": "none", "model": "m"}, [record("a", True, 4, 1000), record("a", False, 6, 3000)])
    data = load_results(path)
    assert data["meta"]["setup"] == "none" and "created" in data["meta"]
    assert data["runs"][0]["total_tokens"] == 1000
    assert summarize(data["runs"])["a"] == {"runs": 2, "passes": 1, "turns": 5, "tokens": 2000, "seconds": 10}


def test_compare_shows_pass_turn_and_token_changes_and_warns_about_dry_runs(tmp_path: Path, capsys):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    write_results(
        before,
        {"setup": "none", "model": "m", "dry_run": False},
        [record("a", False, 10, 4000), record("b", True, 5, 100)],
    )
    write_results(
        after, {"setup": "skill", "model": "m", "dry_run": False}, [record("a", True, 6, 2500), record("c", True, 1, 1)]
    )
    text = compare(before, after)
    assert "differ in setup: none and skill" in text
    assert "task b is in one file only" in text and "task c is in one file only" in text
    line = next(row for row in text.splitlines() if row.startswith("a "))
    assert "0/1->1/1" in line and "-4.0" in line and "-1500" in line
    assert "pass rate: 0/1 -> 1/1" in text
    write_results(after, {"setup": "none", "model": "m", "dry_run": True}, [record("a", True, 6, 0)])
    assert "dry run holds no model data" in compare(before, after)
    assert compare_main([str(before), str(after)]) == 0
    assert "task" in capsys.readouterr().out
    assert compare_main([str(before), str(tmp_path / "missing.json")]) == 1


def run_pytest_eval(*args: str) -> subprocess.CompletedProcess[str]:
    """Start `pytest eval` in a new process with no model key in its environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "eval", "-p", "no:cacheprovider", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_a_real_run_without_a_key_stops_with_a_clear_message_before_any_browser_starts(tmp_path: Path):
    result = run_pytest_eval("--eval-out", str(tmp_path / "r.json"))
    assert result.returncode == 2
    assert "ANTHROPIC_API_KEY is not set" in result.stdout + result.stderr
    assert not (tmp_path / "r.json").exists()  # no run happened
    assert "stagehand" not in result.stderr  # the browser library never started


def test_the_skill_setup_is_skipped_with_a_note_when_the_skill_file_is_absent(tmp_path: Path):
    out = tmp_path / "r.json"
    result = run_pytest_eval(
        "--eval-dry-run", "--eval-setup", "skill", "--eval-skill-path", str(tmp_path / "no" / "SKILL.md"),
        "--eval-tasks", "cart-add", "--eval-repeats", "1", "--eval-out", str(out), "-rs",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout
    assert "1 skipped" in result.stdout
    assert "does not exist" in result.stdout
    assert not out.exists()


def test_the_default_pytest_run_does_not_collect_the_eval():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert "eval/test_tasks.py" not in result.stdout
    assert "tests/test_eval_harness.py" in result.stdout
