"""The processes and files behind the browser lifecycle: its main process, a pidfile and a profile lock.

It never imports Stagehand. The facade starts the browser, and this module answers four
questions about it. Which process is its main process? Is that process still there? How is it
stopped without losing recent profile writes? Who holds a profile?

Shutdown has three steps, each only when the one before did not end Chrome: Chrome is asked to close
itself, its main process gets SIGTERM, and its process group gets SIGKILL.

Safety rule: a process gets a signal only after `psutil` has confirmed two facts. Its creation
time is the one that was recorded, and its command line holds the exact argument
`--user-data-dir=<profile>`. A substring match is never enough.

Windows has no process group and no graceful SIGTERM there: `terminate` is a hard kill.
So on Windows the main process and its children are killed at once, through `psutil`.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import psutil

from .cdp import CdpConnection, CdpError

GRACE = 5.0  # seconds that a stop waits after SIGTERM, before it kills the group
ASK_WAIT = 2.0  # seconds that a stop waits after it asked Chrome to close itself
_POLL = 0.02
_SAME_START = 0.01  # seconds: two creation times this close are the same process


def user_data_argument(profile: Path) -> str:
    """The exact argument that Chrome gets for its profile."""
    return f"--user-data-dir={profile}"


@dataclass(frozen=True, slots=True)
class ChromeProcess:
    """The main process of one Chrome, named so that a reused pid is not mistaken for it."""

    pid: int
    create_time: float
    profile: Path

    @classmethod
    def of(cls, pid: int, profile: Path) -> "ChromeProcess | None":
        """The record for `pid`, or None when it is not a running process that holds `profile`."""
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
        except psutil.Error:
            return None
        found = cls(pid, create_time, profile)
        return found if found.process() is not None else None

    def process(self) -> psutil.Process | None:
        """The live process, only when it is this one: same creation time, exact profile argument."""
        try:
            process = psutil.Process(self.pid)
            if abs(process.create_time() - self.create_time) > _SAME_START:
                return None
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            if user_data_argument(self.profile) not in process.cmdline():
                return None
        except psutil.Error:
            return None
        return process

    @property
    def running(self) -> bool:
        return self.process() is not None

    async def stop(self, grace: float = GRACE) -> bool:
        """SIGTERM to the main process only, wait up to `grace` s, then kill the group.

        Returns True when the process is gone. Does nothing, and returns True, when the
        process is not this Chrome any more.
        """
        process = self.process()
        if process is None:
            return True
        if sys.platform == "win32":
            return await self._stop_windows(process, grace)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)
        if await self.wait_gone(grace):
            return True
        self._kill_group(process)
        return await self.wait_gone(grace)

    def _kill_group(self, process: psutil.Process) -> None:
        """SIGKILL to the whole group, but only a group that this Chrome leads and that is not ours."""
        if self.process() is None:
            return
        try:
            group = os.getpgid(self.pid)
            if group == self.pid and group != os.getpgrp():
                os.killpg(group, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass

    async def _stop_windows(self, process: psutil.Process, grace: float) -> bool:
        with contextlib.suppress(psutil.Error):
            children = process.children(recursive=True)
            process.terminate()
            for child in children:
                with contextlib.suppress(psutil.Error):
                    child.kill()
        return await self.wait_gone(grace)

    async def wait_gone(self, seconds: float) -> bool:
        """Wait up to `seconds` for the process to end. Returns True when it has."""
        deadline = time.monotonic() + seconds
        while self.running:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)
        return True


async def ask_to_close(connection: CdpConnection, chrome: ChromeProcess, wait: float = ASK_WAIT) -> bool:
    """Ask Chrome to close itself, with `Browser.close`, and wait up to `wait` s. True when it is gone.

    This is the orderly exit. A SIGTERM ends Chrome within about 20 ms and loses the cookies that
    Chrome had not yet written, which is up to 30 s of them. localStorage is not affected.
    Measured on macOS, Chrome 157. Whatever remains is stopped with a signal, by `ChromeProcess.stop`.
    """
    with contextlib.suppress(Exception):  # Chrome may be gone already, in any way
        await connection.send("Browser.close")
    return await chrome.wait_gone(wait)


async def browser_pid(connection: CdpConnection) -> int:
    """The main process id, from Chrome itself: the one `browser` entry of `SystemInfo.getProcessInfo`.

    Raises CdpError when Chrome does not answer, and LookupError when the answer has no single entry.
    """
    reply = await connection.send("SystemInfo.getProcessInfo")
    entries = [entry for entry in reply.get("processInfo", []) if entry.get("type") == "browser"]
    if len(entries) != 1 or not isinstance(entries[0].get("id"), int):
        raise LookupError("Chrome did not name exactly one browser process.")
    return entries[0]["id"]


def scan_for_main(profile: Path) -> int | None:
    """The pid of the one process that has the exact argument `--user-data-dir=<profile>` and no `--type=`.

    The helper processes of Chrome have the profile argument too, but each has a `--type=`.
    Returns None when there is not exactly one such process. Never a substring match.
    """
    wanted = user_data_argument(profile)
    found: list[int] = []
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = process.info["cmdline"] or []
        except psutil.Error:
            continue
        if wanted in command and not any(argument.startswith("--type=") for argument in command):
            found.append(process.pid)
    return found[0] if len(found) == 1 else None


async def find_main_process(connection: CdpConnection, profile: Path) -> ChromeProcess | None:
    """The main process of the Chrome that just started on `profile`: from Chrome, else by an exact scan."""
    pid: int | None
    try:
        pid = await browser_pid(connection)
    except (CdpError, LookupError):
        pid = scan_for_main(profile)
    return None if pid is None else ChromeProcess.of(pid, profile)


class Pidfile:
    """A file next to the profile that holds `{pid, create_time, port, profile}` of its Chrome."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, chrome: ChromeProcess, port: int | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            {"pid": chrome.pid, "create_time": chrome.create_time, "port": port, "profile": str(chrome.profile)}
        )
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text)
        os.replace(temporary, self.path)

    def read(self) -> ChromeProcess | None:
        """The recorded Chrome, or None when there is no readable record."""
        try:
            data: Any = json.loads(self.path.read_text())
            return ChromeProcess(int(data["pid"]), float(data["create_time"]), Path(data["profile"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def remove(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    async def kill_leftover(self, profile: Path, grace: float = GRACE) -> bool:
        """Stop a Chrome that an earlier server left on `profile`. Returns True when one was stopped.

        Only the Chrome in the file is touched, and only when it is the same process and still
        holds exactly this profile. The file is removed in every case.
        """
        recorded = self.read()
        self.remove()
        if recorded is None or recorded.profile != profile or not recorded.running:
            return False
        return await recorded.stop(grace)


class ProfileLock:
    """An advisory lock on a file next to the profile, held while a server uses the profile.

    The system drops it when the process ends, even a killed one, so a held lock means a live
    server holds the profile. A Chrome that a dead server left behind is the pidfile's job.
    Chrome does not inherit the descriptor.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting. Returns False when another process holds it."""
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return False
        except BaseException:
            file.close()
            raise
        self._file = file
        return True

    def release(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()  # closing drops the lock; the empty file stays, so that two servers lock the same inode


__all__ = [
    "ASK_WAIT",
    "GRACE",
    "ChromeProcess",
    "Pidfile",
    "ProfileLock",
    "ask_to_close",
    "browser_pid",
    "find_main_process",
    "scan_for_main",
    "user_data_argument",
]
