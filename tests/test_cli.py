"""The command line: flags, the `doctor` command, and a server that starts with a bad setting."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp_types import TextContent
from stagehand import local_browser

from browser_tools_plugin.cli import parse_command, run_doctor
from browser_tools_plugin.facade import find_chrome as find_chrome_in_facade
from browser_tools_plugin.server import create_server
from browser_tools_plugin.settings import BOOLEAN_SETTINGS, CONFIG_FILE, SETTING_NAMES, flag_name, load_settings
from tests.conftest import find_chrome
from tests.test_stdio import INIT, exchange, server_env


def test_serve_is_the_default_command():
    command, flags = parse_command([])
    assert command == "serve"
    assert flags == {}


def test_every_setting_has_a_flag_in_both_spellings():
    for name in SETTING_NAMES:
        assert flag_name(name) == "--" + name.replace("_", "-")
        for spelling in (flag_name(name), "--" + name):
            argv = [spelling] if name in BOOLEAN_SETTINGS else [spelling, "x"]
            assert name in parse_command(argv)[1]


def test_boolean_flags_turn_on_and_off():
    assert parse_command(["--headed"])[1] == {"headed": True}
    assert parse_command(["--no-headed"])[1] == {"headed": False}
    assert parse_command(["--no-quiet-wait"])[1] == {"quiet_wait": False}


def test_value_flags_keep_the_raw_text_so_a_bad_value_is_a_problem_not_a_crash():
    command, flags = parse_command(["doctor", "--run-timeout", "soon", "--groups", "core,debugging"])
    assert command == "doctor"
    assert flags == {"run_timeout": "soon", "groups": "core,debugging"}


def doctor_text(tmp_path: Path, *argv: str, env: dict[str, str] | None = None, chrome: str | None = None):
    out = io.StringIO()
    environ = {"PLUGIN_DATA": str(tmp_path), **(env or {})}
    flags = parse_command(["doctor", *argv])[1]
    code = run_doctor(load_settings(flags, environ), chrome_finder=lambda _p: chrome or _raise(), out=out)
    return code, out.getvalue()


def _raise() -> str:
    raise RuntimeError("Chrome was not found. Install Chrome, or set chrome_path.")


def test_doctor_prints_each_setting_with_its_value_and_source(tmp_path: Path):
    (tmp_path / CONFIG_FILE).write_text('profile = "work"\nrun_timeout = 20\n')
    code, text = doctor_text(tmp_path, "--headed", env={"BROWSER_TOOLS_RUN_TIMEOUT": "30"}, chrome="/x/chrome")
    assert code == 0
    for name in SETTING_NAMES:
        assert name in text
    rows = {line.split()[0]: line for line in text.splitlines() if line.split() and line.split()[0] in SETTING_NAMES}
    assert "work" in rows["profile"]
    assert CONFIG_FILE in rows["profile"]
    assert "30" in rows["run_timeout"]
    assert "environment BROWSER_TOOLS_RUN_TIMEOUT" in rows["run_timeout"]
    assert "true" in rows["headed"]
    assert "flag --headed" in rows["headed"]
    assert "default" in rows["quiet_wait"]
    assert "environment PLUGIN_DATA" in rows["data_dir"]
    assert str(tmp_path / CONFIG_FILE) in text
    assert "Chrome: /x/chrome" in text


def test_doctor_says_what_it_found_when_there_is_no_chrome(tmp_path: Path):
    code, text = doctor_text(tmp_path)
    assert code == 1
    assert "Chrome was not found" in text


def test_doctor_lists_problems_and_fails(tmp_path: Path):
    (tmp_path / CONFIG_FILE).write_text("run_timeout = 0\n")
    code, text = doctor_text(tmp_path, chrome="/x/chrome")
    assert code == 1
    assert "run_timeout" in text
    assert str(tmp_path / CONFIG_FILE) in text


def test_doctor_creates_nothing_and_launches_nothing(tmp_path: Path, monkeypatch):
    async def launch(**_kwargs: object) -> None:
        raise AssertionError("doctor must not launch Chrome")

    monkeypatch.setattr(local_browser, "launch", launch)
    data = tmp_path / "data"
    doctor_text(data, chrome="/x/chrome")
    assert not data.exists()


def test_doctor_finds_the_real_chrome_without_starting_it(tmp_path: Path):
    expected = find_chrome()
    result = subprocess.run(
        [sys.executable, "-c", "from browser_tools_plugin import main; main()", "doctor"],
        env=server_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert "Chrome:" in result.stdout
    if expected is None:
        assert result.returncode == 1
    else:
        assert result.returncode == 0, result.stdout
    assert [p.name for p in tmp_path.iterdir()] == []


async def test_a_bad_setting_leaves_the_tools_listed_and_every_call_one_error(tmp_path: Path, monkeypatch):
    async def launch(**_kwargs: object) -> None:
        raise AssertionError("must not launch")

    monkeypatch.setattr(local_browser, "launch", launch)
    (tmp_path / CONFIG_FILE).write_text("run_timeout = 0\nresult_ceiling = 'many'\n")
    loaded = load_settings({}, {"PLUGIN_DATA": str(tmp_path)})
    async with Client(create_server(loaded.settings, problems=loaded.problems)) as client:
        assert [t.name for t in (await client.list_tools()).tools] == [
            "snapshot",
            "run",
            "screenshot",
            "console",
            "network",
        ]
        for name, args in (("snapshot", {}), ("run", {"code": "1"}), ("console", {}), ("network", {})):
            result = await client.call_tool(name, args)
            assert result.is_error
            (part,) = result.content
            assert isinstance(part, TextContent)
            text = part.text
            assert "run_timeout" in text
            assert "result_ceiling" in text
            assert str(tmp_path / CONFIG_FILE) in text


def test_the_stdio_server_starts_with_a_broken_file_and_answers_with_one_error(tmp_path: Path):
    (tmp_path / CONFIG_FILE).write_text("not [valid")
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "snapshot", "arguments": {}}}
    messages = exchange([*INIT, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, call], tmp_path)
    answer = next(m for m in messages if m.get("id") == 2)
    assert answer["result"]["isError"] is True
    assert str(tmp_path / CONFIG_FILE) in answer["result"]["content"][0]["text"]
    assert len(next(m for m in messages if m.get("id") == 3)["result"]["tools"]) == 5


def test_the_stdio_server_takes_flags(tmp_path: Path):
    """A flag reaches the server: a bad value makes the call fail, naming the flag."""
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "snapshot", "arguments": {}}}
    code = "import sys; from browser_tools_plugin import main; sys.argv[1:] = ['--run-timeout', 'soon']; main()"
    messages = exchange([*INIT, call], tmp_path, [sys.executable, "-c", code])
    answer = next(m for m in messages if m.get("id") == 2)
    assert answer["result"]["isError"] is True
    assert "--run-timeout" in answer["result"]["content"][0]["text"]


def test_find_chrome_rejects_a_missing_explicit_path(tmp_path: Path):
    """Guards the facade's use of the SDK's private finder: it must still raise RuntimeError."""
    with pytest.raises(RuntimeError):
        find_chrome_in_facade(str(tmp_path / "no-such-chrome"))
