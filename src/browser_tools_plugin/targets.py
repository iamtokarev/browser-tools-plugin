"""Attaching to every page and iframe from the moment Chrome creates it.

Capture and the site-limit enforcer both need this. Each one is a listener: it names the
commands to send to a new session and then reads the events of that session. Chrome pauses a
new target until every listener's commands are sent and the target is resumed, so the first
request of a popup or an iframe is seen by every listener.
"""

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any

from .cdp import CdpConnection, CdpError


class TargetListener:
    """What a user of the attach path may implement. Every method has a harmless default."""

    def on_attach(self, session: str, info: dict[str, Any], target_id: str) -> list[Awaitable[Any]]:
        """A new session of the tab `target_id`. Return the commands to send before the target runs.

        `info` is the target info of a page or an iframe. Do not wait for an answer in here.
        """
        return []

    def on_event(self, target_id: str, session: str, method: str, params: dict[str, Any]) -> Awaitable[None] | None:
        """An event of a session that belongs to the tab `target_id`."""
        return None

    def on_target_created(self, info: dict[str, Any]) -> Awaitable[None] | None:
        """Chrome made a new target. Called for every type of target."""
        return None

    def on_target_info(self, info: dict[str, Any]) -> None:
        """A target was made or its title or URL changed."""

    def on_target_destroyed(self, target_id: str) -> None:
        """A target is gone."""


class Targets:
    """Attaches to the tabs that exist, and to each new tab and iframe from its creation."""

    def __init__(self, connection: CdpConnection) -> None:
        self._cdp = connection
        self._listeners: list[TargetListener] = []
        self._owner: dict[str, str] = {}  # session id -> target id of the tab it belongs to
        self._claimed: set[str] = set()  # targets that were attached, or are being attached
        self._started = False

    def add_listener(self, listener: TargetListener) -> None:
        self._listeners.append(listener)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._cdp.add_handler(self._on_event)
        await self._cdp.send("Target.setDiscoverTargets", {"discover": True})
        await self._cdp.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
                "filter": [{"type": "page"}, {"type": "iframe"}, {"type": "service_worker"}, {"exclude": True}],
            },
        )
        targets = (await self._cdp.send("Target.getTargets"))["targetInfos"]
        for info in targets:
            if info["type"] == "page" and info["targetId"] not in self._claimed:
                self._claimed.add(info["targetId"])
                reply = await self._cdp.send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
                await self._setup(reply["sessionId"], info, None, waiting=False)

    def _on_event(self, message: dict[str, Any]) -> Awaitable[None] | None:
        method, params, session = message.get("method"), message.get("params", {}), message.get("sessionId")
        if method == "Target.attachedToTarget":
            return self._on_attached(params, session)
        if method == "Target.targetCreated":
            return self._on_target_created(params["targetInfo"])
        if method == "Target.targetInfoChanged":
            for listener in self._listeners:
                listener.on_target_info(params["targetInfo"])
        elif method == "Target.targetDestroyed":
            for listener in self._listeners:
                listener.on_target_destroyed(params["targetId"])
        elif method == "Target.detachedFromTarget":
            self._owner.pop(params.get("sessionId"), None)
        elif session in self._owner and method:
            return self._on_session_event(self._owner[session], session, method, params)
        return None

    def _on_target_created(self, info: dict[str, Any]) -> Awaitable[None] | None:
        tasks = []
        for listener in self._listeners:
            listener.on_target_info(info)
            outcome = listener.on_target_created(info)
            if outcome is not None:
                tasks.append(outcome)
        return _all(tasks) if tasks else None

    def _on_session_event(
        self, target_id: str, session: str, method: str, params: dict[str, Any]
    ) -> Awaitable[None] | None:
        tasks = []
        for listener in self._listeners:
            outcome = listener.on_event(target_id, session, method, params)
            if outcome is not None:
                tasks.append(outcome)
        return _all(tasks) if tasks else None

    def _on_attached(self, params: dict[str, Any], parent_session: str | None) -> Awaitable[None] | None:
        info = params["targetInfo"]
        waiting = params.get("waitingForDebugger", False)
        if info["type"] not in ("page", "iframe", "service_worker"):
            # The filter keeps these away; if one arrives, never leave it paused.
            return self._resume(params["sessionId"]) if waiting else None
        if info["targetId"] in self._claimed:
            return None  # start() attached this tab itself
        self._claimed.add(info["targetId"])
        return self._setup(params["sessionId"], info, parent_session, waiting=waiting)

    async def _resume(self, session: str) -> None:
        with contextlib.suppress(CdpError):
            await self._cdp.send("Runtime.runIfWaitingForDebugger", session=session)

    async def _setup(self, session: str, info: dict[str, Any], parent_session: str | None, *, waiting: bool) -> None:
        if info["type"] == "service_worker":
            target_id = info["targetId"]  # not a tab; it is its own owner
        elif info["type"] == "page":
            target_id = info["targetId"]
        else:
            target_id = self._owner.get(parent_session or "")
        if target_id is None:
            if waiting:
                await self._resume(session)
            return
        self._owner[session] = target_id
        # Send every command without waiting for each answer, and resume last. A popup opened by
        # `target=_blank` answers no command until it resumes, so waiting for the first answer
        # would stall; resuming first would lose its first events.
        commands: list[Awaitable[Any]] = []
        for listener in self._listeners:
            commands.extend(listener.on_attach(session, info, target_id))
        if info["type"] != "service_worker":
            commands.append(
                self._cdp.send(
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": True,
                        "flatten": True,
                        "filter": [{"type": "iframe"}, {"exclude": True}],
                    },
                    session=session,
                )
            )
        if waiting:
            commands.append(self._cdp.send("Runtime.runIfWaitingForDebugger", session=session))
        await asyncio.gather(*commands, return_exceptions=True)


async def _all(outcomes: list[Awaitable[None]]) -> None:
    await asyncio.gather(*outcomes)
