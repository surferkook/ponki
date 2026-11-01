"""The door for robots (hub/serve.py): the body port answers bodies and nothing else."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hub.serve import BodyGate, token_problem

TOKEN = "a-long-random-token-for-the-tests-0123456789"


class BodyPortTest(unittest.TestCase):
    """TestClient talks to port 80, so a gate on port 80 is the body port, on 81 it's the house port."""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from hub.main import create_app

        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        app = create_app(data_dir=data, token=TOKEN)
        (data / "voice").mkdir(exist_ok=True)
        (data / "voice" / "7.wav").write_bytes(b"RIFF....WAVE")
        cls.body = TestClient(BodyGate(app, 80, TOKEN), raise_server_exceptions=False)
        cls.house = TestClient(BodyGate(app, 81, TOKEN), raise_server_exceptions=False)
        cls.auth = {"Authorization": f"Bearer {TOKEN}"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_the_web_ui_and_api_are_not_behind_the_body_port(self):
        for path in ("/", "/api/info", "/api/robots", "/api/system", "/api/events"):
            self.assertEqual(self.body.get(path, headers=self.auth).status_code, 404, path)
        self.assertEqual(self.body.post("/api/builds", json={}, headers=self.auth).status_code, 404)

    def test_the_house_port_still_has_everything(self):
        self.assertEqual(self.house.get("/").status_code, 200)
        self.assertEqual(self.house.get("/api/info").status_code, 200)
        self.assertEqual(self.house.get("/api/voice/7.wav").status_code, 200)

    def test_speakers_fetch_voice_clips_with_the_token(self):
        ok = self.body.get("/api/voice/7.wav", headers=self.auth)
        self.assertEqual((ok.status_code, ok.content[:4]), (200, b"RIFF"))
        self.assertEqual(self.body.get("/api/voice/7.wav").status_code, 404)
        self.assertEqual(self.body.get("/api/voice/7.wav",
                                       headers={"Authorization": "Bearer wrong"}).status_code, 404)

    def test_bodies_reach_the_websocket_and_the_hub_checks_their_token(self):
        from starlette.websockets import WebSocketDisconnect

        with self.body.websocket_connect("/ws/body") as ws:
            ws.send_json({"type": "hello", "token": "wrong", "manifest": {"name": "probe"}})
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_json()
        self.assertEqual(caught.exception.code, 4401)  # the hub's own check, past the gate

    def test_the_ui_websocket_is_not_behind_the_body_port(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.body.websocket_connect("/ws/ui") as ws:
                ws.receive_json()


class TokenTest(unittest.TestCase):
    def test_a_local_body_port_takes_any_token(self):
        for bind in ("", "127.0.0.1", "localhost", "::1"):
            self.assertIsNone(token_problem("ponki-in-a-box", bind))

    def test_an_open_body_port_needs_a_real_token(self):
        for token in ("", "ponki-in-a-box", "change-me-to-a-long-random-string", "short"):
            self.assertIsNotNone(token_problem(token, "0.0.0.0"), token)
        self.assertIsNotNone(token_problem("x" * 23, "192.0.2.7"))
        self.assertIsNone(token_problem(TOKEN, "0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
