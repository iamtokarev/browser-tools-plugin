"""Downloads: every file lands in the download directory, under a name that the server chose.

Two things keep the files in place. Before the launch, `seed_download_directory` writes the
directory into the profile's preferences, so a download lands there even when no CDP command
is in force. After the launch, the host connection sets `allowAndName`: Chrome then saves
each file under its GUID, and the server renames it to a safe, unique name and reports the path.
Page code can re-point the directory over its own CDP connection, so the host connection sets
it again before every tool call. See the spec, section 4.2, "Downloads".

The server never opens or runs a download, and it sets no execute bit.
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cdp import CdpConnection
from .sites import SiteLimits

DOWNLOAD_WAIT = 10.0  # seconds that a tool call waits for a download that is in progress
REPORT_LIMIT = 50  # finished downloads that are kept between two tool calls
NOTICE_LIMIT = 5  # downloads that one notice lists
NAME_BYTES = 120  # the longest file name, without the number that makes it unique
FALLBACK_NAME = "download"

NEUTRAL_SUFFIX = ".download"

# Extensions that a system can launch, or run, when a person opens the file. The server names the
# saved file itself, so Chrome's own checks for dangerous files do not apply. A name with one of
# these extensions gets NEUTRAL_SUFFIX. The list is a denylist, in lower case, of the executable
# and launchable types of macOS, Linux and Windows. It is wider than "executable" on purpose:
# a script, an installer, a shortcut or a settings file all start something.
RISKY_EXTENSIONS = frozenset(
    {
        # macOS
        "app", "command", "terminal", "scpt", "scptd", "applescript", "osa", "workflow", "action", "wflow",
        "tool", "pkg", "mpkg", "dmg", "prefpane", "saver", "plugin", "kext", "webloc", "inetloc", "fileloc",
        # Linux and other Unix
        "sh", "bash", "zsh", "csh", "ksh", "fish", "run", "bin", "appimage", "desktop", "deb", "rpm",
        "snap", "flatpakref", "flatpakrepo",
        # Windows
        "exe", "com", "bat", "cmd", "msi", "msp", "msix", "appx", "ps1", "psm1", "ps1xml", "psc1", "vbs",
        "vbe", "vb", "js", "jse", "wsf", "wsh", "hta", "scr", "pif", "lnk", "reg", "cpl", "msc", "url",
        "dll", "sys", "inf", "gadget", "application", "appref-ms", "chm", "jnlp", "library-ms", "settingcontent-ms",
        # Java, on every system
        "jar",
    }
)  # fmt: skip

_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]+')


def safe_name(suggested: str) -> str:
    """A file name that cannot leave the directory, from a name that a page or a server chose.

    Only the last path part is kept (both kinds of separator). Control characters and the
    characters that some systems refuse are replaced. A leading dot is removed, so the file
    is never hidden and the name is never `..`. The name is cut to `NAME_BYTES` bytes and keeps its extension.
    """
    last = re.split(r"[\\/]", suggested)[-1]
    name = _UNSAFE.sub("_", last).strip().lstrip(".").strip()
    stem, extension = os.path.splitext(name)
    extension = extension[:16]
    stem = _cut(stem, NAME_BYTES - len(extension.encode()))
    return f"{stem}{extension}" if stem else FALLBACK_NAME


def neutral_name(name: str) -> tuple[str, bool]:
    """`name`, or `name.download` when its extension is in RISKY_EXTENSIONS (any case).

    Returns the name and whether it changed. A trailing dot or space is ignored when the
    extension is read, because Windows ignores it too.
    """
    extension = os.path.splitext(name.rstrip(". "))[1].lstrip(".").lower()
    if extension in RISKY_EXTENSIONS:
        return name + NEUTRAL_SUFFIX, True
    return name, False


def _cut(text: str, limit: int) -> str:
    return text.encode()[: max(limit, 1)].decode(errors="ignore").rstrip()


def unique_path(directory: Path, name: str) -> Path:
    """`directory/name`, or `directory/stem (1).ext` and so on, for a name that is taken."""
    stem, extension = os.path.splitext(name)
    candidate = directory / name
    number = 0
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = directory / f"{stem} ({number}){extension}"
    return candidate


def seed_download_directory(profile_dir: Path, download_dir: Path) -> None:
    """Write the download directory into the preferences of the profile at `profile_dir`.

    Run it before Chrome starts, on the dedicated profile and on a throwaway one. An older value in
    an existing profile is replaced, and the other preferences stay. A file that cannot be read as
    JSON is replaced: it holds nothing that a person would keep.
    """
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    path = default_dir / "Preferences"
    preferences: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            preferences = loaded
    download = preferences.get("download")
    preferences["download"] = {
        **(download if isinstance(download, dict) else {}),
        "default_directory": str(download_dir),
        "prompt_for_download": False,
        "directory_upgrade": True,
    }
    savefile = preferences.get("savefile")
    preferences["savefile"] = {
        **(savefile if isinstance(savefile, dict) else {}),
        "default_directory": str(download_dir),
    }
    handle, temporary = tempfile.mkstemp(dir=default_dir, prefix="Preferences.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            json.dump(preferences, file)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@dataclass(frozen=True, slots=True)
class Download:
    """The result of one download, for the agent."""

    url: str  # as the agent may read it: no query
    path: Path | None  # None when it did not finish
    size: int = 0
    outside: bool = False  # the file is not in the download directory, so the server left it where it is
    neutralised: bool = False  # the name got NEUTRAL_SUFFIX, because its extension can launch a program


@dataclass(slots=True)
class _Pending:
    url: str
    suggested: str


@dataclass(slots=True)
class _State:
    pending: dict[str, _Pending] = field(default_factory=dict)  # by GUID
    finished: list[Download] = field(default_factory=list)
    idle: asyncio.Event = field(default_factory=asyncio.Event)


def downloads_notice(downloads: list[Download], still_running: int = 0) -> str:
    """One paragraph for the end of a tool result that says where the downloads were saved."""
    parts = []
    for download in downloads[:NOTICE_LIMIT]:
        if download.path is None:
            parts.append(f"A download from {download.url} did not finish. No file was saved.")
        elif download.outside:
            parts.append(
                f"Downloaded: {download.path} ({download.size} bytes) from {download.url}. "
                "This is not the download directory, and the server left the file there."
            )
        else:
            line = f"Downloaded: {download.path} ({download.size} bytes) from {download.url}."
            if download.neutralised:
                line += (
                    f" The server added {NEUTRAL_SUFFIX} to the name, because the file type can start a program. "
                    "Rename the file only if the user asks."
                )
            parts.append(line)
    more = len(downloads) - NOTICE_LIMIT
    if more > 0:
        parts.append(f"{more} more downloads finished. Look in the download directory.")
    if still_running:
        parts.append(f"{still_running} download(s) still running. A later result gives the path.")
    if not parts:
        return ""
    if downloads:
        parts.append("The server saves a file only. It does not open or run it.")
    return " ".join(parts)


class Downloads:
    """Sets the download behaviour of the browser, names the saved files and keeps the reports."""

    def __init__(self, connection: CdpConnection, directory: Path) -> None:
        self._cdp = connection
        self._directory = directory
        self._state = _State()
        self._state.idle.set()
        self._started = False

    async def start(self) -> None:
        """Set the behaviour and listen for the events. Raises CdpError when Chrome refuses."""
        if not self._started:
            self._started = True
            self._cdp.add_handler(self._on_event)
        await self.assert_directory()

    async def assert_directory(self) -> None:
        """Set the behaviour again. The last client that sets it wins, and page code can be that client."""
        await self._cdp.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allowAndName", "downloadPath": str(self._directory), "eventsEnabled": True},
        )

    async def settle(self, timeout: float = DOWNLOAD_WAIT) -> tuple[list[Download], int]:
        """The downloads that finished since the last call, and how many are still running.

        Waits up to `timeout` s for the ones that are running.
        """
        if not self._state.idle.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._state.idle.wait(), timeout)
        finished, self._state.finished = self._state.finished, []
        return finished, len(self._state.pending)

    def _on_event(self, message: dict[str, Any]) -> Awaitable[None] | None:
        method, params = message.get("method"), message.get("params", {})
        if method == "Browser.downloadWillBegin":
            self._state.pending[params["guid"]] = _Pending(
                SiteLimits.describe_url(params.get("url", "")), str(params.get("suggestedFilename", ""))
            )
            self._state.idle.clear()
        elif method == "Browser.downloadProgress" and params.get("state") in ("completed", "canceled"):
            return self._finish(params)
        return None

    async def _finish(self, params: dict[str, Any]) -> None:
        guid = params["guid"]
        pending = self._state.pending.pop(guid, None)
        try:
            if pending is None:
                return  # one that started before the server listened
            if params["state"] != "completed":
                self._keep(Download(pending.url, None))
                return
            try:
                self._keep(await asyncio.to_thread(self._name_file, guid, pending, params.get("filePath")))
            except OSError as error:
                print(f"browser-tools: a download could not be renamed: {error!r}", file=sys.stderr, flush=True)
                self._keep(Download(pending.url, None))
        finally:
            if not self._state.pending:
                self._state.idle.set()

    def _keep(self, download: Download) -> None:
        if len(self._state.finished) < REPORT_LIMIT:
            self._state.finished.append(download)

    def _name_file(self, guid: str, pending: _Pending, reported: str | None) -> Download:
        """Move the saved file to its own safe, unique name. Only a file in the download directory is moved."""
        source = Path(reported) if reported else self._directory / guid
        directory = self._directory
        if source.parent.resolve() != directory.resolve() or source.is_symlink():
            size = source.stat().st_size if source.is_file() else 0
            return Download(pending.url, source, size, outside=True)
        size = source.stat().st_size
        name, neutralised = neutral_name(safe_name(pending.suggested))
        while True:
            target = unique_path(directory, name)
            try:
                os.link(source, target)  # fails when the name is taken, so nothing is overwritten
            except FileExistsError:
                continue
            except OSError:
                if target.exists():
                    continue
                os.rename(source, target)
                break
            os.unlink(source)
            break
        return Download(pending.url, target, size, neutralised=neutralised)


__all__ = ["Download", "Downloads", "downloads_notice", "neutral_name", "safe_name", "seed_download_directory"]
