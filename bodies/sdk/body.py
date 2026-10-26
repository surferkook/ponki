"""The body SDK: turns any Python process into a Ponki body (spec 01).

A body dials the hub, declares a manifest of actions and runs them on a
device. Only dependency is ``websockets``. Runs on Python 3.10 and 3.11.

    from body import Body, Image
    body = Body("desk-lamp", "A small lamp on the desk.")
    @body.action("light", "Set the lamp colour.", {"color": {...}})
    async def light(color): ...
    body.run()
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("body")


@dataclass
class Image:
    """Return from an action to send a JPEG photo to the hub."""

    jpeg: bytes
    caption: str = ""


class Body:
    def __init__(
        self,
        name: str,
        description: str,
        *,
        voice: str | None = None,
        voice_style: str | None = None,
        persona_hint: str | None = None,
        display_name: str | None = None,
        personality: str | None = None,
        kind: str | None = None,
        hub: str | None = None,
        token: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.voice = voice
        self.voice_style = voice_style
        self.persona_hint = persona_hint
        self.display_name = display_name
        self.personality = personality
        self.kind = kind
        hub = hub or os.environ.get("HOUSE_HUB") or "ws://localhost:8200"
        hub = hub.rstrip("/")
        if not hub.endswith("/ws/body"):
            hub = hub + "/ws/body"
        self.hub_url = hub
        self.token = token if token is not None else os.environ.get("HOUSE_TOKEN", "")
        self._actions: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable] = {}
        self.state: dict[str, Any] = {}
        self._ws: Any = None
        self._welcomed = False
        self._logger = logging.getLogger(f"body.{name}")

    # -- declaring actions -------------------------------------------------
    def action(
        self,
        name: str,
        description: str,
        params: dict | None = None,
        *,
        required: list[str] | None = None,
        returns: str | None = None,
        hidden: bool = False,
        sound: bool = False,
    ):
        properties = dict(params or {})
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "required": list(required) if required is not None else list(properties.keys()),
        }
        entry: dict[str, Any] = {"description": description, "params": schema}
        if returns:
            entry["returns"] = returns
        if hidden:
            entry["hidden"] = True
        if sound:
            entry["sound"] = True

        def deco(fn: Callable) -> Callable:
            self._actions[name] = entry
            self._handlers[name] = fn
            return fn

        return deco

    def manifest(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "protocol": 1,
            "name": self.name,
            "description": self.description,
            "actions": self._actions,
            "state": dict(self.state),
        }
        if self.display_name:
            m["display_name"] = self.display_name
        if self.voice:
            m["voice"] = self.voice
        if self.voice_style:
            m["voice_style"] = self.voice_style
        if self.persona_hint:
            m["persona_hint"] = self.persona_hint
        if self.personality:
            m["personality"] = self.personality
        if self.kind:
            m["kind"] = self.kind
        return m

    # -- talking to the hub ------------------------------------------------
    async def _send(self, obj: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(obj))
        except Exception:
            pass  # silently dropped while disconnected

    async def set_state(self, **changes: Any) -> None:
        changed = {k: v for k, v in changes.items() if self.state.get(k) != v}
        if not changed:
            return
        self.state.update(changed)
        await self._send({"type": "state", "state": dict(self.state)})

    async def tell(self, text: str, from_human: bool = False) -> None:
        msg: dict[str, Any] = {"type": "input", "text": text}
        if from_human:
            msg["from"] = "human"
        await self._send(msg)

    # -- act handling -------------------------------------------------------
    async def _handle_act(self, msg: dict[str, Any]) -> None:
        act_id = str(msg.get("id", ""))
        action = msg.get("action", "")
        args = msg.get("args") or {}
        fn = self._handlers.get(action)
        if fn is None:
            await self._send({"type": "result", "id": act_id, "ok": False,
                              "error": f"unknown action '{action}'"})
            return
        try:
            result = fn(**args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # plain words: the soul reads this
            await self._send({"type": "result", "id": act_id, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"})
            return
        if isinstance(result, Image):
            await self._send({
                "type": "result", "id": act_id, "ok": True,
                "data": result.caption or "here's what I see",
                "image": base64.b64encode(result.jpeg).decode("ascii"),
            })
        elif result is None:
            await self._send({"type": "result", "id": act_id, "ok": True, "data": "done"})
        else:
            data = result if isinstance(result, (str, dict, list, int, float, bool)) else str(result)
            await self._send({"type": "result", "id": act_id, "ok": True, "data": data})

    async def _session(self) -> bool:
        """One connection. Returns True if we got a welcome."""
        import websockets

        got_welcome = False
        async with websockets.connect(self.hub_url, max_size=16 * 1024 * 1024,
                                      ping_interval=20) as ws:
            self._ws = ws
            await ws.send(json.dumps({"type": "hello", "token": self.token,
                                      "manifest": self.manifest()}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "welcome":
                raise RuntimeError(f"hub did not welcome us: {msg!r}")
            got_welcome = True
            self._welcomed = True
            self._logger.info("welcomed as %s by %s", msg.get("name"), self.hub_url)
            if self.state:
                await ws.send(json.dumps({"type": "state", "state": dict(self.state)}))
            tasks: set[asyncio.Task] = set()
            try:
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(frame, dict) and frame.get("type") == "act":
                        task = asyncio.ensure_future(self._handle_act(frame))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            finally:
                for task in tasks:
                    task.cancel()
                self._ws = None
        return got_welcome

    async def run_async(self, *background: Any) -> None:
        for bg in background:
            asyncio.ensure_future(bg() if callable(bg) and not inspect.iscoroutine(bg) else bg)
        delay = 1.0
        while True:
            self._welcomed = False
            try:
                await self._session()
                self._welcomed = True  # a clean close still had its welcome
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.info("hub unreachable (%s) — retrying in %gs",
                                  exc, round(delay))
            if self._welcomed:  # the hub restarted: start again at 1 s
                delay = 1.0
            await asyncio.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, 30.0)

    def run(self, *background: Any) -> None:
        logging.basicConfig(format="%(asctime)s %(name)s %(message)s",
                            level=logging.INFO)
        asyncio.run(self.run_async(*background))
