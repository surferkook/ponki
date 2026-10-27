"""RoBoHoN driver and family tests (spec 08): a fake control server."""
from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HOUSE_TOKEN", "test")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "robohon"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies"))

import robohon_body as driver  # noqa: E402
from families import robohon as family  # noqa: E402


def _wav(seconds: float = 0.1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


class FakeServer:
    def __init__(self) -> None:
        self.spoken: list[dict] = []
        self.looks = 0
        self.log = ["I/ROBOHON-POSE( 1): getPose -> pose=519 state=0",
                    "boot ok"]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/gestures":
                    self._json([{"key": "wave_hello", "desc": "wave a hand"},
                                {"key": "walk", "desc": "walk"}])
                elif self.path == "/api/status":
                    self._json({"online": True, "via": "bridge", "model": "SR01MW"})
                elif self.path == "/audio/rb.wav":
                    data = _wav()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/devicelog":
                    data = "\n".join(outer.log).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/pose":
                    self._json({"pose": "stand", "name": "stand", "raw": 0})
                elif self.path == "/vision/latest.jpg":
                    data = b"\xff\xd8fake-jpeg"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/speak":
                    outer.spoken.append(payload)
                    self._json({"ok": True, "moved": False})
                elif self.path.startswith("/api/gesture/"):
                    if self.path.endswith("walk_back"):
                        self._json({"ok": False, "pose": "cradle",
                                    "reason": "no 'walk_back' variant playable while cradle"})
                    else:
                        outer.log.append("I/x: startMotion(1) ret=0")
                        self._json({"ok": True, "id": "abc", "pose": "stand"})
                elif self.path == "/api/look":
                    outer.looks += 1
                    self._json({"ok": outer.looks > 1,
                                "url": "/vision/latest.jpg?v=1"})
                else:
                    self.send_response(404)
                    self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.web = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()


class Voices(unittest.TestCase):
    def test_picks(self) -> None:
        self.assertEqual(driver.pick_voice("Здраво, како си?"), "styletts2:dzambazov")
        self.assertEqual(driver.pick_voice("ik ben blij, dank je"), "nl_NL-alex-medium")
        self.assertEqual(driver.pick_voice("hello there, how are you"), "kokoro:af_heart")

    def test_split(self) -> None:
        pieces = driver.split_lines("A. " * 200)
        self.assertTrue(all(len(p) <= 300 for p in pieces))
        self.assertGreater(len(pieces), 1)


class Driver(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeServer()
        self.robot = driver.Robohon(self.fake.web)

    def tearDown(self) -> None:
        self.fake.close()

    def test_gestures_menu(self) -> None:
        gestures = asyncio.run(self.robot.gestures())
        self.assertEqual([g["key"] for g in gestures], ["wave_hello", "walk"])

    def test_speak_waits_and_splits(self) -> None:
        import time
        start = time.monotonic()
        asyncio.run(self.robot.speak("One. " * 100))  # ~500 chars → 2 pieces
        self.assertEqual(len(self.fake.spoken), 2)
        self.assertGreaterEqual(time.monotonic() - start, 1.6)  # 2 × (0.8 + wav)

    def test_gesture_ok_and_refused(self) -> None:
        self.assertEqual(asyncio.run(self.robot.gesture("wave_hello")),
                         "playing wave_hello")
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(self.robot.gesture("walk_back"))
        self.assertIn("while cradle", str(ctx.exception))

    def test_look_retries(self) -> None:
        image = asyncio.run(self.robot.look())
        self.assertEqual(self.fake.looks, 2)
        self.assertEqual(image.jpeg, b"\xff\xd8fake-jpeg")

    def test_pose_legs(self) -> None:
        data = asyncio.run(self.robot.pose())
        self.assertEqual(driver.legs_for(data["pose"]), "free: you can walk")
        self.assertIn("locked", driver.legs_for("cradle"))
        self.assertEqual(driver.legs_for("sit"), "not standing")


class Family(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeServer()
        os.environ["HOUSE_ROBOHON_WEB"] = self.fake.web

    def tearDown(self) -> None:
        self.fake.close()
        os.environ.pop("HOUSE_ROBOHON_WEB", None)

    def test_discover(self) -> None:
        found = family.discover()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["ready"])
        self.assertEqual(found[0]["gestures"], 2)
        self.assertEqual(found[0]["status"], "online (bridge)")

    def test_discover_nothing(self) -> None:
        os.environ["HOUSE_ROBOHON_WEB"] = "http://127.0.0.1:9"
        self.assertEqual(family.discover(), [])

    def test_example_and_checks(self) -> None:
        device = family.EXAMPLE["device"]
        self.assertEqual(device["gestures"], 56)
        kinds = list(family.checks({}, device))
        self.assertEqual(kinds, ["voice", "gesture", "camera"])
        self.assertEqual(family.problems({}), ([], []))
        body = family.body({}, device)
        self.assertTrue(any("56 gestures" in row[1] for row in body["rows"]))
        result = asyncio.run(family.check(device, {}, "camera"))
        self.assertIn("image", result)


if __name__ == "__main__":
    unittest.main()
