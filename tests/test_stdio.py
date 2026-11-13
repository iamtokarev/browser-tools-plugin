"""The real command over real stdio: tool listing, stdout hygiene, one launched snapshot."""

import json
import subprocess
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters
from mcp_types import TextContent

from tests.conftest import needs_chrome

COMMAND = [sys.executable, "-c", "from browser_tools_plugin import main; main()"]


def server_env(data_dir: Path) -> dict[str, str]:
    import os

    return {**os.environ, "PLUGIN_DATA": str(data_dir)}


def chrome_processes_using(path: Path) -> list[str]:
    out = subprocess.run(["pgrep", "-f", f"user-data-dir={path}"], capture_output=True, text=True).stdout
    return out.split()


INIT = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw", "version": "0"}},
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def exchange(requests: list[dict], data_dir: Path, command: list[str] | None = None) -> list[dict]:
    """Send requests, wait for one answer per request id, close stdin, return every stdout line
    parsed as JSON. A stray non-JSON byte on stdout fails the parse."""
    wanted = sum(1 for r in requests if "id" in r)
    proc = subprocess.Popen(
        command or COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=server_env(data_dir),
    )
    assert proc.stdin is not None and proc.stdout is not None
    lines: list[str] = []
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        while len(lines) < wanted:
            line = proc.stdout.readline()
            assert line, "server closed stdout early"
            lines.append(line)
        proc.stdin.close()
        lines += proc.stdout.read().splitlines(keepends=True)
        proc.wait(timeout=30)
    finally:
        proc.kill()
        proc.stdout.close()
    return [json.loads(line) for line in lines]


def test_stdout_carries_only_mcp_messages_and_listing_starts_no_browser(tmp_path: Path):
    messages = exchange([*INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], tmp_path)
    assert all(m.get("jsonrpc") == "2.0" for m in messages)
    assert [m["id"] for m in messages if "id" in m] == [1, 2]
    assert list(tmp_path.iterdir()) == []  # no profile directory: nothing launched


@needs_chrome
def test_stdout_stays_clean_while_chrome_launches_and_snapshots(tmp_path: Path):
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "snapshot", "arguments": {}}}
    messages = exchange([*INIT, call], tmp_path)
    assert all(m.get("jsonrpc") == "2.0" for m in messages)
    answer = next(m for m in messages if m.get("id") == 2)
    assert answer["result"]["content"][0]["text"].startswith("Page: ")
    assert (tmp_path / "profiles" / "default").is_dir()  # it did launch this time
    assert chrome_processes_using(tmp_path) == []  # and shut down at stdin EOF


async def test_lists_tools_over_stdio_without_launching_chrome(tmp_path: Path):
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:], env=server_env(tmp_path))
    with anyio.fail_after(60):
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == ["snapshot", "run", "console"]
            assert client.server_info is not None
            assert client.server_info.name == "browser-tools"
            assert chrome_processes_using(tmp_path) == []


@needs_chrome
async def test_snapshot_over_stdio_launches_chrome_lazily_and_closes_it(tmp_path: Path):
    params = StdioServerParameters(command=COMMAND[0], args=COMMAND[1:], env=server_env(tmp_path))
    with anyio.fail_after(90):
        async with Client(params) as client:
            assert chrome_processes_using(tmp_path) == []
            result = await client.call_tool("snapshot", {})
            assert not result.is_error
            (part,) = result.content
            assert isinstance(part, TextContent)
            assert part.text.startswith("Page: ")
            assert chrome_processes_using(tmp_path) != []
    # Closing the client ends stdin; the server shuts the browser down.
    for _ in range(50):
        if not chrome_processes_using(tmp_path):
            break
        await anyio.sleep(0.2)
    assert chrome_processes_using(tmp_path) == []


def test_uv_run_browser_tools_serves_tools_without_a_browser(tmp_path: Path):
    """The documented entry point: `uv run browser-tools`."""
    global COMMAND
    saved, COMMAND = COMMAND, ["uv", "run", "browser-tools"]
    try:
        messages = exchange([*INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], tmp_path)
    finally:
        COMMAND = saved
    assert [t["name"] for t in messages[1]["result"]["tools"]] == ["snapshot", "run", "console"]
    assert list(tmp_path.iterdir()) == []
