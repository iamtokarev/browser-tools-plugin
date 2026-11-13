"""The server's own connection to Chrome's DevTools protocol.

It talks to the Chrome that this server launched, at the address Stagehand reports. It does
not import Stagehand, so any later feature on the host connection can share it.
"""

import asyncio
import contextlib
import itertools
import json
import sys
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]

COMMAND_TIMEOUT = 10.0


class CdpError(Exception):
    """Chrome answered a command with an error, or did not answer."""


def _websocket_url(http_url: str) -> str:
    with urllib.request.urlopen(http_url.rstrip("/") + "/json/version", timeout=COMMAND_TIMEOUT) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class CdpConnection:
    """One browser-level websocket. Sessions are flat: a message names its session.

    A handler gets every event. A handler may return a coroutine, which then runs as its own
    task, so that a slow handler does not hold up the reading of events.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: list[Handler] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._socket: websockets.ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._on_close: list[Callable[[], None]] = []

    @classmethod
    async def connect(cls, http_url: str) -> "CdpConnection":
        """Open the connection to the Chrome whose debugging address is `http_url`."""
        connection = cls()
        url = await asyncio.to_thread(_websocket_url, http_url)
        connection._socket = await websockets.connect(url, max_size=None, ping_interval=None)
        connection._reader = asyncio.create_task(connection._read())
        return connection

    @property
    def open(self) -> bool:
        return self._socket is not None and self._reader is not None and not self._reader.done()

    def add_handler(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Call `callback` when the connection ends for any reason, including `close`."""
        self._on_close.append(callback)

    async def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None) -> Any:
        """Send one command and wait for its result. Raises CdpError on an error or a timeout."""
        if self._socket is None or not self.open:
            raise CdpError("The connection to the browser is closed.")
        command_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        message: dict[str, Any] = {"id": command_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        try:
            await self._socket.send(json.dumps(message))
            return await asyncio.wait_for(future, COMMAND_TIMEOUT)
        except TimeoutError:
            raise CdpError(f"{method} did not answer within {COMMAND_TIMEOUT:g} s.") from None
        except websockets.ConnectionClosed:
            raise CdpError("The connection to the browser is closed.") from None
        finally:
            self._pending.pop(command_id, None)

    async def close(self) -> None:
        """Close the websocket and stop reading. Never raises."""
        socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _read(self) -> None:
        try:
            assert self._socket is not None
            async for raw in self._socket:
                self._dispatch(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("The connection to the browser is closed."))
            for callback in self._on_close:
                callback()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(CdpError(str(message["error"].get("message", "unknown error"))))
                else:
                    future.set_result(message.get("result", {}))
            return
        for handler in self._handlers:
            outcome = handler(message)
            if asyncio.iscoroutine(outcome):
                task = asyncio.create_task(outcome)
                self._tasks.add(task)
                task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            print(f"browser-tools: an event handler failed: {task.exception()!r}", file=sys.stderr, flush=True)
