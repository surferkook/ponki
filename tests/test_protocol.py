"""Body protocol tests (spec 01/02): close codes, acts, state, SDK errors."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

from body import Body  # noqa: E402
from hub.house import House  # noqa: E402
from hub.main import create_app  # noqa: E402


class StubProvider:
    model = "muse-spark-1.3"
    name = "meta"


MANIFEST = {"protocol": 1, "name": "tester", "display_name": "Tester",
            "description": "A fake test robot.",
            "actions": {"wave": {"description": "Wave.",
                                 "params": {"type": "object",
                                            "properties": {"times": {"type": "integer"}},
                                            "required": ["times"],
                                            "additionalProperties": False}}},
            "state": {"awake": True}}


class FakeWS:
    """A body end of the wire: answers acts the way the SDK would."""

    def __init__(self, house: House) -> None:
        self.house = house
        self.closed_code = None
        self.replies = 0

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, payload) -> None:
        if payload.get("type") == "act":
            async def reply() -> None:
                await asyncio.sleep(0)
                self.replies += 1
                await self.house.on_body_message(
                    "tester", {"type": "result", "id": payload["id"], "ok": True,
                               "data": f"waved {payload['args'].get('times', 1)} times"})
            asyncio.ensure_future(reply())


class HouseProtocol(unittest.TestCase):
    def test_act_result_matched_by_id_and_state_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = House(StubProvider(), "Darko", Path(tmp))

            async def run() -> None:
                ws = FakeWS(house)
                key = await house.attach(ws, dict(MANIFEST))
                self.assertEqual(key, "tester")
                self.assertIn("tester", house.manifests)
                result = await house.act("tester", "wave", {"times": 2})
                self.assertEqual(result["text"], "waved 2 times")
                self.assertEqual(ws.replies, 1)  # exactly one result
                await house.on_body_message("tester", {"type": "state",
                                                       "state": {"awake": False}})
                self.assertFalse(house.bodies["tester"].state["awake"])
                missing = await house.act("tester", "dance", {})
                self.assertIn("no action 'dance'", missing["text"])

            asyncio.run(run())

    def test_killing_body_mid_act_fails_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = House(StubProvider(), "Darko", Path(tmp))

            class SilentWS(FakeWS):
                async def send_json(self, payload) -> None:
                    pass  # never replies

            async def run() -> None:
                ws = SilentWS(house)
                await house.attach(ws, dict(MANIFEST))
                task = asyncio.ensure_future(house.act("tester", "wave", {"times": 1}))
                await asyncio.sleep(0.05)
                house.detach("tester", ws)
                result = await asyncio.wait_for(task, timeout=2)
                self.assertFalse(result["ok"])
                self.assertIn("disconnected", result["text"])

            asyncio.run(run())

    def test_offline_body_plain_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = House(StubProvider(), "Darko", Path(tmp))

            async def run() -> None:
                result = await house.act("tester", "wave", {})
                self.assertEqual(result["text"], "your body is offline")

            asyncio.run(run())


class SdkErrors(unittest.TestCase):
    def test_action_that_raises_and_unknown_action(self) -> None:
        sent: list = []
        body = Body("boomer", "It fails.", hub="ws://localhost:8200", token="x")

        @body.action("boom", "Fail.", {})
        async def boom():
            raise ValueError("servo stalled")

        async def capture(obj) -> None:
            sent.append(obj)

        body._send = capture  # capture instead of a socket

        async def run() -> None:
            await body._handle_act({"id": "19", "action": "boom", "args": {}})
            await body._handle_act({"id": "20", "action": "nope", "args": {}})

        asyncio.run(run())
        self.assertFalse(sent[0]["ok"])
        self.assertIn("servo stalled", sent[0]["error"])
        self.assertFalse(sent[1]["ok"])

    def test_schema_wrapping(self) -> None:
        body = Body("lamp", "A lamp.", hub="ws://localhost:8200", token="x")

        @body.action("light", "Set it.", {"color": {"type": "string"}})
        async def light(color):
            return color

        schema = body.manifest()["actions"]["light"]["params"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["color"])  # every property by default
        self.assertFalse(schema["additionalProperties"])


class HandshakeCodes(unittest.TestCase):
    def test_close_codes_and_welcome(self) -> None:
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=Path(tmp), token="sekret")
            client = TestClient(app, raise_server_exceptions=False)

            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect("/ws/body") as ws:
                    ws.send_json({"type": "hello", "token": "wrong", "manifest": MANIFEST})
                    ws.receive_json()
            self.assertEqual(ctx.exception.code, 4401)

            bad = dict(MANIFEST, name="bad name!")
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect("/ws/body") as ws:
                    ws.send_json({"type": "hello", "token": "sekret", "manifest": bad})
                    ws.receive_json()
            self.assertEqual(ctx.exception.code, 4400)

            with client.websocket_connect("/ws/body") as ws:
                ws.send_json({"type": "hello", "token": "sekret", "manifest": MANIFEST})
                welcome = ws.receive_json()
            self.assertEqual(welcome, {"type": "welcome", "name": "tester"})
            self.assertIn("tester", app.state.house.manifests)


if __name__ == "__main__":
    unittest.main()
