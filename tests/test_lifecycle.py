"""The process and file parts of the browser lifecycle, with stand-in processes instead of Chrome.

A stand-in is a Python process whose command line holds `--user-data-dir=<profile>`, so that the
checks of `lifecycle` see what they see on a real Chrome. Only processes that a test started
get a signal, and each test ends them.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from browser_tools_plugin import lifecycle
from browser_tools_plugin.cdp import CdpError
from browser_tools_plugin.lifecycle import (
    ChromeProcess,
    Pidfile,
    ProfileLock,
    ask_to_close,
    browser_pid,
    find_main_process,
    scan_for_main,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins use POSIX signals")

SLEEP = "import time; time.sleep(60)"
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
WITH_CHILD = (
    "import signal, subprocess, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "print(child.pid, flush=True); time.sleep(60)"
)


class Spawner:
    def __init__(self) -> None:
        self.started: list[subprocess.Popen[str]] = []

    def start(self, code: str, *arguments: str, own_session: bool = True) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", code, *arguments],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=own_session,
        )
        self.started.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # the command line is final once the interpreter runs
            if psutil.Process(process.pid).cmdline()[-1:] == list(arguments[-1:]):
                break
            time.sleep(0.01)
        time.sleep(0.2)  # the signal handler of the stand-in is in place
        return process

    def stop_all(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()


@pytest.fixture
def spawn() -> Iterator[Spawner]:
    spawner = Spawner()
    yield spawner
    spawner.stop_all()


def chrome_of(process: subprocess.Popen[str], profile: Path) -> ChromeProcess:
    found = ChromeProcess.of(process.pid, profile)
    assert found is not None
    return found


# --- Stopping ---------------------------------------------------------------------------------


async def test_a_stop_sends_sigterm_to_the_main_process_and_waits_for_it(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    chrome = chrome_of(process, profile)
    started = time.monotonic()
    assert await chrome.stop()
    assert time.monotonic() - started < 2
    assert process.wait(timeout=5) == -signal.SIGTERM


async def test_a_stop_kills_the_group_when_sigterm_is_ignored(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(WITH_CHILD, f"--user-data-dir={profile}")
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    chrome = chrome_of(process, profile)
    assert await chrome.stop(grace=0.3)
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not psutil.pid_exists(child_pid)  # the group went, not only the main process


async def test_a_process_in_our_own_group_is_killed_alone_and_never_the_group(
    tmp_path: Path, spawn: Spawner, monkeypatch: pytest.MonkeyPatch
):
    profile = tmp_path / "p"
    process = spawn.start(STUBBORN, f"--user-data-dir={profile}", own_session=False)
    assert os.getpgid(process.pid) == os.getpgrp()
    group_kills: list[Any] = []
    monkeypatch.setattr(lifecycle.os, "killpg", lambda *args: group_kills.append(args))
    assert await chrome_of(process, profile).stop(grace=0.3)
    assert group_kills == []  # the guard held: our own group is never signalled
    assert process.wait(timeout=5) == -signal.SIGKILL


async def test_a_process_that_is_not_this_chrome_gets_no_signal(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    chrome = chrome_of(process, profile)
    reused = ChromeProcess(chrome.pid, chrome.create_time + 100, profile)  # the pid of a later process
    other_profile = ChromeProcess(chrome.pid, chrome.create_time, tmp_path / "other")
    assert not reused.running
    assert not other_profile.running
    assert await reused.stop() and await other_profile.stop()
    assert process.poll() is None


async def test_a_stop_of_a_process_that_is_gone_succeeds(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    chrome = chrome_of(process, profile)
    process.kill()
    process.wait()
    assert await chrome.stop()


# --- Finding the main process -----------------------------------------------------------------


def test_the_scan_matches_the_exact_argument_and_skips_helper_processes(tmp_path: Path, spawn: Spawner):
    profile, sibling = tmp_path / "p", tmp_path / "p2"
    main = spawn.start(SLEEP, f"--user-data-dir={profile}")
    spawn.start(SLEEP, "--type=renderer", f"--user-data-dir={profile}")
    spawn.start(SLEEP, f"--user-data-dir={sibling}")
    assert scan_for_main(profile) == main.pid
    assert scan_for_main(tmp_path / "none") is None


def test_the_scan_gives_nothing_when_two_processes_could_be_the_main_one(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    spawn.start(SLEEP, f"--user-data-dir={profile}")
    spawn.start(SLEEP, f"--user-data-dir={profile}")
    assert scan_for_main(profile) is None


class StubConnection:
    def __init__(self, reply: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.reply, self.error = reply or {}, error

    async def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> Any:
        assert method == "SystemInfo.getProcessInfo"
        if self.error:
            raise self.error
        return self.reply


def process_info(*types: str) -> dict[str, Any]:
    return {"processInfo": [{"type": kind, "id": 100 + index} for index, kind in enumerate(types)]}


async def test_the_main_pid_is_the_single_browser_entry():
    connection: Any = StubConnection(process_info("gpu", "browser", "renderer"))
    assert await browser_pid(connection) == 101


@pytest.mark.parametrize("types", [(), ("gpu",), ("browser", "browser")])
async def test_no_single_browser_entry_is_an_error(types: tuple[str, ...]):
    connection: Any = StubConnection(process_info(*types))
    with pytest.raises(LookupError):
        await browser_pid(connection)


async def test_the_scan_is_the_fallback_when_chrome_does_not_answer(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    connection: Any = StubConnection(error=CdpError("no answer"))
    found = await find_main_process(connection, profile)
    assert found is not None and found.pid == process.pid


async def test_chrome_is_asked_first(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    connection: Any = StubConnection({"processInfo": [{"type": "browser", "id": process.pid}]})
    found = await find_main_process(connection, profile)
    assert found == chrome_of(process, profile)


# --- The pidfile --------------------------------------------------------------------------------


async def test_a_leftover_chrome_in_the_pidfile_is_stopped(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    pidfile = Pidfile(tmp_path / "p.pid")
    pidfile.write(chrome_of(process, profile), port=9222)
    assert pidfile.read() == chrome_of(process, profile)
    assert await pidfile.kill_leftover(profile)
    assert process.wait(timeout=5) == -signal.SIGTERM
    assert not pidfile.path.exists()


async def test_a_pidfile_whose_process_is_another_one_stops_nothing(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    pidfile = Pidfile(tmp_path / "p.pid")
    real = chrome_of(process, profile)
    pidfile.write(ChromeProcess(real.pid, real.create_time + 50, profile), port=None)  # a reused pid
    assert not await pidfile.kill_leftover(profile)
    assert process.poll() is None
    assert not pidfile.path.exists()


async def test_a_pidfile_for_another_profile_stops_nothing(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    pidfile = Pidfile(tmp_path / "q.pid")
    pidfile.write(chrome_of(process, profile), port=None)
    assert not await pidfile.kill_leftover(tmp_path / "q")
    assert process.poll() is None


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"pid": "x"}'])
async def test_a_broken_pidfile_is_ignored_and_removed(tmp_path: Path, content: str):
    pidfile = Pidfile(tmp_path / "p.pid")
    pidfile.path.write_text(content)
    assert not await pidfile.kill_leftover(tmp_path / "p")
    assert not pidfile.path.exists()


# --- The profile lock ---------------------------------------------------------------------------


def test_a_second_holder_of_the_lock_is_refused_until_the_first_lets_go(tmp_path: Path):
    first, second = ProfileLock(tmp_path / "p.lock"), ProfileLock(tmp_path / "p.lock")
    assert first.acquire()
    assert first.acquire()  # again, by the same holder
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_the_lock_of_a_killed_process_is_free(tmp_path: Path):
    path = tmp_path / "p.lock"
    code = (
        "import sys, time; from pathlib import Path; "
        "from browser_tools_plugin.lifecycle import ProfileLock; "
        "lock = ProfileLock(Path(sys.argv[1])); print(lock.acquire(), flush=True); time.sleep(60)"
    )
    holder = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "True"
        assert not ProfileLock(path).acquire()
        holder.kill()
        holder.wait()
        lock = ProfileLock(path)
        assert lock.acquire()
        lock.release()
    finally:
        holder.kill()
        holder.wait()
        if holder.stdout:
            holder.stdout.close()


# --- The orderly exit ---------------------------------------------------------------------------------


class ClosingConnection:
    """A stand-in for Chrome's side of `Browser.close`: it ends the process, or does not."""

    def __init__(self, process: subprocess.Popen[str] | None, error: Exception | None = None) -> None:
        self.process, self.error = process, error
        self.sent: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> Any:
        self.sent.append(method)
        if self.error:
            raise self.error
        if self.process:
            self.process.terminate()
        return {}


async def test_chrome_is_asked_to_close_and_the_wait_ends_when_it_is_gone(tmp_path: Path, spawn: Spawner):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    connection = ClosingConnection(process)
    assert await ask_to_close(connection, chrome_of(process, profile))  # ty: ignore[invalid-argument-type]
    assert connection.sent == ["Browser.close"]


async def test_a_chrome_that_does_not_close_is_left_for_the_signal_and_the_error_is_not_raised(
    tmp_path: Path, spawn: Spawner
):
    profile = tmp_path / "p"
    process = spawn.start(SLEEP, f"--user-data-dir={profile}")
    connection = ClosingConnection(None, CdpError("closed"))
    assert not await ask_to_close(connection, chrome_of(process, profile), wait=0.2)  # ty: ignore[invalid-argument-type]
    assert process.poll() is None
