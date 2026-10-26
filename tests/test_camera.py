"""The camera: head geometry, WS-Security, discovery, the Stream (spec 05).

No camera, no network: fake captures, fake demuxers, canned SOAP.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "onvif"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "families"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

import onvif_body  # noqa: E402
import onvif_client  # noqa: E402
from onvif_body import Stream, heading_text, move_units, record_clip  # noqa: E402
from onvif_client import OnvifError, parse_probe_matches, wsse_digest, wsse_header  # noqa: E402

C225 = {"pan_degrees": 328, "tilt_degrees": 96, "pan_sign": -1,
        "backlash": 0.016, "home": [0.0, 0.0]}


class HeadGeometry(unittest.TestCase):
    def test_degrees_to_units_with_tapo_sign(self) -> None:
        last: dict = {}
        ux, uy = move_units(45, 0, C225, last)
        self.assertAlmostEqual(ux, -0.274390, places=5)  # 2*45/328, sign -1
        self.assertEqual(last["pan"], -1)

    def test_backlash_only_on_reversal(self) -> None:
        last: dict = {}
        move_units(45, 0, C225, last)
        ux, _ = move_units(45, 0, C225, last)  # same way: no backlash
        self.assertAlmostEqual(ux, -0.274390, places=5)
        ux, _ = move_units(-45, 0, C225, last)  # reverse: backlash added
        self.assertAlmostEqual(ux, 0.274390 + 0.016, places=5)
        ux, _ = move_units(0, 0, C225, last)  # a zero move changes nothing
        self.assertEqual(ux, 0)
        self.assertEqual(last["pan"], 1)

    def test_absolute_move_resets_reversal_memory(self) -> None:
        last: dict = {}
        move_units(45, 0, C225, last)
        last = {}  # what look_home does after an AbsoluteMove
        ux, _ = move_units(-45, 0, C225, last)
        self.assertAlmostEqual(ux, 0.274390, places=5)  # no backlash

    def test_heading_words(self) -> None:
        # 12° right (pan_sign -1 inverts x), 3° up from home
        text = heading_text((-12 * 2 / 328, 3 * 2 / 96), C225)
        self.assertEqual(text, "12° right and 3° up from your home view")
        text = heading_text((12 * 2 / 328, -3 * 2 / 96), C225)
        self.assertEqual(text, "12° left and 3° down from your home view")
        self.assertEqual(heading_text((0.0, 0.0), C225), "at your home view")
        no_home = dict(C225, home=None)
        self.assertEqual(heading_text((0.5, -0.5), no_home), "pan -82°, tilt -24°")


class WsSecurity(unittest.TestCase):
    def test_digest_matches_sha1_formula(self) -> None:
        nonce = b"0123456789abcdef"
        created = "2026-10-26T10:00:00.000Z"
        expected = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + b"secret").digest()).decode()
        self.assertEqual(wsse_digest(nonce, created, "secret"), expected)

    def test_header_escapes_and_uses_camera_clock(self) -> None:
        now = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
        header = wsse_header("a&b<c", "pw", clock_offset=3600,
                             nonce=b"0123456789abcdef", now=now)
        self.assertIn("a&amp;b&lt;c", header)
        self.assertIn("2026-10-26T11:00:00.000Z", header)  # +1h camera offset
        self.assertIn(wsse_digest(b"0123456789abcdef",
                                  "2026-10-26T11:00:00.000Z", "pw"), header)


class ClockSync(unittest.TestCase):
    def test_structured_onvif_datetime(self) -> None:
        import xml.etree.ElementTree as ET

        raw = b"""<Envelope><Body><GetSystemDateAndTimeResponse>
        <UTCDateTime><Date><Year>2026</Year><Month>10</Month><Day>26</Day></Date>
        <Time><Hour>10</Hour><Minute>30</Minute><Second>0</Second></Time>
        </UTCDateTime></GetSystemDateAndTimeResponse></Body></Envelope>"""
        cam = onvif_client.Camera("http://x/onvif/device_service", "u", "p")
        cam._post = lambda url, body, auth=True: ET.fromstring(raw)
        offset = cam.sync_clock()
        self.assertIsInstance(offset, float)  # camera time parsed, offset kept


class DiscoveryParsing(unittest.TestCase):
    def test_probe_match_prefers_sender_xaddr(self) -> None:
        raw = b"""<?xml version="1.0"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
          xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
        <s:Body><d:ProbeMatches><d:ProbeMatch>
          <a:XAddrs xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">http://10.0.0.9/onvif/device_service http://192.168.1.39:2020/onvif/device_service</a:XAddrs>
          <d:Scopes>onvif://www.onvif.org/name/Tapo%20C225 onvif://www.onvif.org/hardware/C225</d:Scopes>
        </d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""
        found = parse_probe_matches(raw, sender="192.168.1.39")
        self.assertEqual(found[0]["host"], "192.168.1.39")
        self.assertIn("192.168.1.39", found[0]["xaddr"])
        self.assertEqual(found[0]["name"], "Tapo C225")

    def test_tapo_model_from_reverse_name(self) -> None:
        import net as net_lib

        self.assertEqual(net_lib.tapo_model_from_name("c225.home"), "C225")
        self.assertEqual(net_lib.tapo_model_from_name("printer.home"), "")


class FakeCap:
    """Returns frames from a script: [(delay_before, frame-or-None), ...]."""

    def __init__(self, script):
        self.script = list(script)
        self.released = False

    def read(self):
        if self.script:
            delay, frame = self.script.pop(0)
            if delay:
                time.sleep(delay)
            if frame is None:
                return False, None
            return True, frame
        time.sleep(0.05)
        return False, None

    def release(self):
        self.released = True


class StreamFreshness(unittest.TestCase):
    def test_frame_from_before_request_is_never_returned(self) -> None:
        # frame A lands at open (first second: skipped anyway), B at ~1.3 s
        cap = FakeCap([(0, b"old"), (1.3, b"fresh"), (0, b"fresh")])
        stream = Stream("rtsp://x", capture_factory=lambda url: cap)
        try:
            self.assertEqual(stream.jpeg(), b"fresh")
        finally:
            stream.stop()

    def test_stalled_stream_reopens(self) -> None:
        self.addCleanup(setattr, onvif_body, "STALL", onvif_body.STALL)
        onvif_body.STALL = 0.6
        cap1 = FakeCap([(1.1, b"one"), (0, b"one")])  # then silence
        cap2 = FakeCap([(1.1, b"two"), (0, b"two")])
        caps = iter([cap1, cap2])
        stream = Stream("rtsp://x", capture_factory=lambda url: next(caps))
        try:
            self.assertEqual(stream.jpeg(), b"one")
            self.assertEqual(stream.jpeg(), b"two")  # reopened after the stall
        finally:
            stream.stop()

    def test_old_generation_cannot_overwrite(self) -> None:
        cap = FakeCap([(0, b"stale")])
        stream = Stream("rtsp://x", capture_factory=lambda url: cap)
        stream.stop()  # generation now ahead of any old reader
        gen = stream._generation - 1
        stream._reader(gen, cap)  # returns at once, stores nothing
        self.assertIsNone(stream._frame)

    def test_dead_stream_plain_words(self) -> None:
        self.addCleanup(setattr, onvif_body, "OPEN_WAIT", onvif_body.OPEN_WAIT)
        onvif_body.OPEN_WAIT = 0.4
        stream = Stream("rtsp://x", capture_factory=lambda url: FakeCap([]))
        try:
            with self.assertRaises(OnvifError) as ctx:
                stream.jpeg()
            self.assertIn("gives no new picture", str(ctx.exception))
        finally:
            stream.stop()

    def test_caption_time_from_decode(self) -> None:
        cap = FakeCap([(1.1, b"x")])
        stream = Stream("rtsp://x", capture_factory=lambda url: cap)
        try:
            stream.jpeg()
            self.assertAlmostEqual(stream.shot_at, time.time(), delta=5)
            self.assertRegex(onvif_body.caption_time(stream.shot_at),
                             r"^\d{2}:\d{2}:\d{2}$")
        finally:
            stream.stop()


class FakePacket:
    def __init__(self, key=False):
        self.is_keyframe = key
        self.pts = None
        self.dts = None
        self.stream = None


class RecordClip(unittest.TestCase):
    def test_keyframe_even_pts_and_arrival_times(self) -> None:
        packets = [FakePacket(), FakePacket(key=True), FakePacket(), FakePacket()]
        written: list = []

        class InContainer:
            streams = SimpleNamespace(video=[object()])

            def demux(self, stream):
                yield from packets

            def close(self):
                pass

        class OutContainer:
            def __init__(self, path):
                self.path = path

            def add_stream_from_template(self, stream):
                return "out-stream"

            def mux(self, packet):
                written.append((packet.pts, packet.dts))

            def close(self):
                Path(self.path).write_bytes(b"MP4")

        def fake_open(target, mode=None, format=None, options=None):
            if mode == "w":
                return OutContainer(target)
            return InContainer()

        ticks = iter([0.0, 0.0, 0.5, 3.0])
        fake_av = SimpleNamespace(open=fake_open)
        result = record_clip("rtsp://x", 2, av_module=fake_av,
                             clock=lambda: next(ticks))
        self.assertEqual(result["frames"], 2)  # pre-keyframe dropped, 3 s stops
        self.assertEqual(written, [(0, 0), (3600, 3600)])  # evenly spaced 25 fps
        self.assertEqual(result["times"], [0.0, 0.5])  # arrival times, 3 dp
        self.assertEqual(base64.b64decode(result["video"]), b"MP4")


class FamilyShape(unittest.TestCase):
    def test_example_checks_body_command(self) -> None:
        import onvif as family

        ids = [c["id"] for c in family.checks({}, family.EXAMPLE["device"])]
        self.assertEqual(ids, ["camera", "pan", "tilt", "home"])
        text = family.body({}, family.EXAMPLE["device"])
        self.assertIn("home view | remembered", text)
        cmd, env = family.command("/tmp/keiki.json", {})
        self.assertTrue(cmd[-1].endswith("onvif_body.py") or
                        cmd[-2].endswith("onvif_body.py"))
        self.assertEqual(family.head_for("C225")["pan_sign"], -1)
        self.assertEqual(family.head_for("Other")["pan_degrees"], 360)


class DriverManifest(unittest.TestCase):
    def test_actions_and_captions(self) -> None:
        config = {"name": "keiki", "display_name": "Keiki",
                  "description": "A camera.",
                  "device": dict(C225, id="onvif:192.0.2.39", host="192.0.2.39",
                                 ptz=True)}
        body, robot = onvif_body.build_body(config, Path("/tmp/keiki.json"))
        manifest = body.manifest()
        self.assertIn("look", manifest["actions"])
        self.assertIn("turn", manifest["actions"])
        self.assertIn("look_home", manifest["actions"])
        self.assertTrue(manifest["actions"]["record"]["hidden"])
        self.assertEqual(manifest["actions"]["look"]["returns"], "image")
        self.assertIn("328°", manifest["actions"]["turn"]["description"])
        self.assertIn("drift back to your home view",
                      manifest["actions"]["turn"]["description"])


if __name__ == "__main__":
    unittest.main()
