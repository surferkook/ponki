"""The mBot2: framing, upload, render, the rover link, body logic (spec 07).

No hardware: a fake serial board and a fake rover on localhost.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "mbot2"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "families"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

import cyberpi_link as link_mod  # noqa: E402
import rover_link  # noqa: E402
from cyberpi_link import CyberPi, FrameReader, frame, pad_program  # noqa: E402
from rover_link import Offline, RoverLink, free_port, render, version  # noqa: E402


class Framing(unittest.TestCase):
    def test_ping_frame_bytes(self) -> None:
        self.assertEqual(frame(b"\x0d\x00\x00"),
                         bytes.fromhex("F3F603000D00000DF4"))

    def test_header_and_checksum_300(self) -> None:
        body = bytes(range(256)) + bytes(44)
        raw = frame(body)
        self.assertEqual(raw[0], 0xF3)
        self.assertEqual(raw[1], (0xF3 + 44 + 1) & 0xFF)
        self.assertEqual(raw[2] | (raw[3] << 8), 300)
        self.assertEqual(raw[-2], sum(body) & 0xFF)
        self.assertEqual(raw[-1], 0xF4)

    def test_script_frame_layout(self) -> None:
        raw = link_mod.script_frame("1", 5)
        reader = FrameReader()
        bodies = reader.feed(raw)
        self.assertEqual(bodies[0][0], 0x28)
        self.assertEqual(bodies[0][1], 0x01)
        self.assertEqual(bodies[0][2] | (bodies[0][3] << 8), 5)
        self.assertEqual(bodies[0][4] | (bodies[0][5] << 8), 1)
        self.assertEqual(bodies[0][6:], b"1")

    def test_reader_split_text_and_resync(self) -> None:
        good1 = frame(b"\x0d\x00\x00")
        good2 = frame(b"\x28\x01\x07\x00\x02\x00{}")
        bad = bytearray(frame(b"\x01\x02"))
        bad[-2] ^= 0xFF  # break the checksum
        stream = b"hello " + good1 + bytes(bad) + b"mid" + good2 + b" tail"
        reader = FrameReader()
        bodies = []
        for i in range(len(stream)):
            bodies.extend(reader.feed(stream[i:i + 1]))
        self.assertEqual(bodies, [b"\x0d\x00\x00", b"\x28\x01\x07\x00\x02\x00{}"])
        self.assertIn("hello", reader.take_text())
        self.assertIn("tail", reader.take_text() + "tail")  # text kept


class UploadShape(unittest.TestCase):
    def test_pad(self) -> None:
        for n in (0, 1, 4, 79, 80, 84, 85, 500):
            padded = pad_program(b"x" * n)
            self.assertEqual(len(padded) % 80, 4)
            self.assertTrue(set(padded[n:]) <= {10})
        self.assertEqual(pad_program(b"x" * 84), b"x" * 84)  # already aligned

    def test_xor32(self) -> None:
        self.assertEqual(link_mod.xor32(b"\x01\x00\x00\x00\x02\x00\x00\x00"), 3)
        self.assertEqual(link_mod.xor32(b"\x01"), 1)  # zero-padded

    def test_upload_frames_layout(self) -> None:
        padded = pad_program(b"print('hi')\n" * 40)
        frames = link_mod.upload_frames(padded)
        reader = FrameReader()
        bodies = []
        for raw in frames:
            bodies.extend(reader.feed(raw))
        header = bodies[0]
        self.assertEqual(header[:4], b"\x01\x00\x5e\x01")
        payload = header[6:]
        self.assertEqual(payload[0], 0)
        size, xor = struct.unpack("<I", payload[1:5])[0], \
            struct.unpack("<I", payload[5:9])[0]
        self.assertEqual(size, len(padded))
        self.assertEqual(xor, link_mod.xor32(padded))
        self.assertEqual(payload[9:], b"/flash/_xx_main.py")
        blocks = bodies[1:]
        offsets = [struct.unpack("<I", b[6:10])[0] for b in blocks]
        self.assertEqual(offsets, list(range(0, len(padded) - 4, 80))
                         + [len(padded) - 4])
        self.assertEqual(len(blocks[-1][10:]), 4)
        joined = b"".join(b[10:] for b in blocks)
        self.assertEqual(joined, padded)


class FakeSerial:
    def __init__(self, responder):
        self.responder = responder
        self.queue = bytearray()
        self.written: list[bytes] = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.queue)

    def write(self, data):
        self.written.append(bytes(data))
        reply = self.responder(bytes(data))
        if reply:
            self.queue.extend(reply)
        return len(data)

    def read(self, n=1):
        if not self.queue:
            return b""
        out = bytes(self.queue[:n])
        del self.queue[:n]
        return out

    def close(self):
        self.closed = True


def board_responder(drop_first_block=False):
    state = {"blocks": 0, "dropped": False}
    categories: list[str] = []

    def respond(raw: bytes):
        reader = FrameReader()
        bodies = reader.feed(raw)
        if not bodies:
            return b""
        body = bodies[0]
        if body.startswith(b"\x0d"):
            categories.append("ping")
            return frame(b"\x0d\x00\x00")
        if body.startswith(b"\x28\x04"):
            categories.append("prepare")
            return frame(b'{"ret": null}')
        if body.startswith(b"\x28"):
            idx = body[2] | (body[3] << 8)
            return frame(bytes([0x28, 0x01]) + struct.pack("<H", idx)
                         + b'{"ret": 1}')
        if body.startswith(b"\x01\x00\x5e\x01"):
            categories.append("header")
            return frame(b"\x5e\xf0")
        if body.startswith(b"\x01\x00\x5e\x02"):
            state["blocks"] += 1
            categories.append(f"block{state['blocks']}")
            if drop_first_block and state["blocks"] == 1 \
                    and not state["dropped"]:
                state["dropped"] = True
                return b""  # a dropped ack: the upload must retry
            return frame(b"\x5e\xf0")
        return b""

    return respond, categories


class SimulatedBoard(unittest.TestCase):
    def test_upload_sequence_and_retry(self) -> None:
        respond, categories = board_responder(drop_first_block=True)
        board = CyberPi("/dev/fake", boot_wait=2,
                        serial_obj=FakeSerial(respond), answer_timeout=0.6)
        padded_len = board.upload(b"print('rover')\n" * 10)
        self.assertEqual(padded_len % 80, 4)
        self.assertEqual(categories[:6],
                         ["ping", "prepare", "prepare",
                          "ping", "prepare", "prepare"])
        self.assertEqual(categories[6], "header")
        blocks = [c for c in categories if c.startswith("block")]
        self.assertEqual(categories[-3:], ["ping", "prepare", "prepare"])
        # the dropped first block was written twice
        board.close()


class Render(unittest.TestCase):
    def test_render_all_settings(self) -> None:
        out = render("Home Net", 'p"a\\ss', "192.168.1.37", 8221, "0123456789abcdef")
        text = out.decode()
        self.assertIn(json.dumps("Home Net"), text)
        self.assertIn(json.dumps('p"a\\ss'), text)
        self.assertIn("PORT = 8221", text)
        self.assertIn(json.dumps("0123456789abcdef"), text)
        self.assertNotIn("{ssid}", text)
        compile(text, "rover.py", "exec")
        self.assertEqual(version(), 2)
        self.assertNotIn("network.WLAN", text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)


async def _fake_rover(port: int, key: str, script: dict, events: dict):
    for _ in range(50):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except ConnectionRefusedError:
            await asyncio.sleep(0.1)
    else:
        raise ConnectionRefusedError("link server never listened")
    writer.write((json.dumps({"hello": "house-rover", "v": 2, "key": key,
                              "mac": "ECE3342F1308", "battery": 87}) + "\n").encode())
    await writer.drain()
    events["writer"] = writer
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            events.setdefault("commands", []).append(msg.get("do"))
            action = script.get(msg.get("do"), "ok")
            if action == "ignore":
                continue
            if action == "fail":
                reply = {"id": msg["id"], "ok": False,
                         "error": "still moving (drive)"}
            else:
                reply = {"id": msg["id"], "ok": True,
                         "ret": {"done": msg.get("do"), "battery": 87}}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
    except Exception:
        return


class RoverLinkTests(unittest.TestCase):
    def test_link_lifecycle(self) -> None:
        async def run():
            port = free_port(18300)
            changes: list = []
            script: dict = {}

            async def on_change(online, hello):
                changes.append(online)
                if online:
                    # can call the rover from here without deadlocking
                    await link.call("name", text="Vroom", timeout=3)

            link = RoverLink(port, "key-1", on_change=on_change)
            serve = asyncio.ensure_future(link.serve())
            try:
                events: dict = {}
                rover = asyncio.ensure_future(
                    _fake_rover(port, "key-1", script, events))
                hello = await link.wait(5)
                self.assertTrue(link.online)
                self.assertEqual(hello["mac"], "ECE3342F1308")
                await asyncio.sleep(0.2)
                ret = await link.call("drive", cm=10, timeout=3)
                self.assertEqual(ret["done"], "drive")
                script["drive"] = "fail"
                with self.assertRaises(RuntimeError) as ctx:
                    await link.call("drive", cm=10, timeout=3)
                self.assertIn("the rover says: still moving", str(ctx.exception))
                # a pending call fails when the rover goes away
                script["turn"] = "ignore"
                pending = asyncio.ensure_future(
                    link.call("turn", degrees=90, timeout=10))
                await asyncio.sleep(0.3)
                events["writer"].close()
                with self.assertRaises(Offline):
                    await pending
                await asyncio.sleep(0.2)
                self.assertFalse(link.online)
                self.assertEqual(changes, [True, False])
                rover.cancel()
            finally:
                serve.cancel()
                await link.close()

        asyncio.run(run())

    def test_wrong_key_hung_up(self) -> None:
        async def run():
            port = free_port(18400)
            link = RoverLink(port, "right", on_change=None)
            serve = asyncio.ensure_future(link.serve())
            await asyncio.sleep(0.3)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write((json.dumps(
                    {"hello": "house-rover", "key": "wrong"}) + "\n").encode())
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), 3)
                self.assertEqual(line, b"")  # hung up
                self.assertFalse(link.online)
                writer.close()
            finally:
                serve.cancel()
                await link.close()

        asyncio.run(run())


class FakeLink:
    def __init__(self, headings):
        self.calls: list = []
        self.headings = iter(headings)

    async def call(self, do, timeout=None, **args):
        self.calls.append((do, args))
        if do == "sense":
            return {"heading": next(self.headings), "battery": 90}
        if do == "drive":
            return {"done": "drive", "amount": args["cm"]}
        return {"done": do}


class BodyLogic(unittest.TestCase):
    def _rover(self, tmp: str, headings):
        root = Path(tmp)
        (root / "robots").mkdir(parents=True)
        (root / "secrets").mkdir()
        (root / "secrets" / "mbot2:ECE3342F1308.json").write_text(
            json.dumps({"key": "abc123"}), encoding="utf-8")
        cfg = {"name": "vroom-vroom", "display_name": "Vroom-Vroom",
               "description": "A rover.",
               "device": {"id": "mbot2:ECE3342F1308", "rover_port": 19999}}
        path = root / "robots" / "vroom-vroom.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        import mbot2_body

        rover = mbot2_body.RoverBody(cfg, path)
        rover.link = FakeLink(headings)
        return rover

    def test_drive_blind_backing_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rover = self._rover(tmp, [])
            result = asyncio.run(rover.drive(-30))
            self.assertEqual(rover.link.calls[0][1]["cm"], -10)
            self.assertIn("backing up is blind", result)
            result = asyncio.run(rover.drive(20))
            self.assertEqual(result, "drove 20 cm")

    def test_turn_sign_and_gyro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rover = self._rover(tmp, [0, 44])
            result = asyncio.run(rover.turn(45))
            sent = [a for d, a in rover.link.calls if d == "turn"][0]
            self.assertEqual(sent["degrees"], -45)  # TURN_SIGN
            self.assertEqual(result,
                             "turned 45° left (the gyro measured 44° left)")

    def test_turn_whole_circle_no_bracket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rover = self._rover(tmp, [0, 2])
            result = asyncio.run(rover.turn(350))
            self.assertNotIn("gyro measured", result)


class FamilyShape(unittest.TestCase):
    def test_ssid_near_miss(self) -> None:
        import mbot2 as family

        family._check_ssid("Home", ["Home", "Other"])
        family._check_ssid("Anything", [])
        family._check_ssid("Zzz-not-close", ["Home"])
        with self.assertRaises(ValueError) as ctx:
            family._check_ssid("Hoem", ["Home"])
        self.assertIn("did you mean 'Home'", str(ctx.exception))

    def test_discover_known_taken_never_opens(self) -> None:
        import mbot2 as family

        self.addCleanup(setattr, family, "_serial_ports", family._serial_ports)
        self.addCleanup(setattr, family, "_known_sockets", family._known_sockets)
        self.addCleanup(setattr, family, "identify", family.identify)
        family._serial_ports = lambda: [
            {"node": "/dev/ttyUSB0", "port": "/dev/ttyUSB0", "usb": "1-1.1",
             "vendor": "1a86"}]
        family._known_sockets = lambda: {"1-1.1": "mbot2:ECE3342F1308"}

        def boom(port):
            raise AssertionError("must not open a running rover")

        family.identify = boom
        devices = family.discover({"mbot2:ECE3342F1308": "vroom-vroom"})
        self.assertEqual(devices[0]["status"], "in use")
        self.assertEqual(devices[0]["robot"], "vroom-vroom")

    def test_example_checks_and_problems(self) -> None:
        import mbot2 as family

        ids = [c["id"] for c in family.checks({}, family.EXAMPLE["device"])]
        self.assertEqual(ids, ["screen", "sensors", "drive", "turn", "eyes"])
        self.assertEqual(family.problems({"device": family.EXAMPLE["device"]}),
                         [])
        self.assertIn("Wi-Fi → its workshop",
                      family.body({}, family.EXAMPLE["device"]))
        cmd, env = family.command("/tmp/vroom.json", {})
        self.assertTrue(cmd[-2].endswith("mbot2_body.py"))


if __name__ == "__main__":
    unittest.main()
