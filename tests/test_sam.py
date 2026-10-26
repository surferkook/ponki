"""The finding service: SAM grammar, mask fit, motion, words (spec 05).

No network, no model: canned SAM answers and synthetic masks/boxes.
The SAM server is imported from services/sam (its own venv deps: numpy,
Pillow, fastapi, httpx — all present in the hub venv for these tests).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "sam"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import sam_server as sam  # noqa: E402
from hub import grounding  # noqa: E402

IMAGE_ANSWER = (
    '7<|box;x1=100;y1=50;x2=300;y2=250;w=1000;h=500;c=0.92|>'
    '<|mask;x=0;y=0;data=200,200,~abcDEF|>,'
    '8<|box;x1=10;y1=10;x2=60;y2=60;w=1000;h=500;c=0.20|>'
    '<|mask;x=0;y=0;data=50,50,!xyz|>'
)
VIDEO_ANSWER = (
    '<0f>3<|box;x1=0;y1=0;x2=100;y2=100;w=640;h=480;c=0.9|>'
    '<|mask;x=0;y=0;data=100,100,~aaa|>\n'
    '<5f>3<|box;x1=10;y1=0;x2=110;y2=100;w=640;h=480;c=0.9|>'
    '<|mask;x=0;y=0;data=100,100,~bbb|>'
)


REAL_ANSWER = (
    '<0f>0<|box;x1=863;y1=119;x2=1008;y2=258;w=1024;h=1024;'
    'c=0.8439024686813354|><|mask;x=0;y=0;c=0.8439024686813354;'
    'data=140,146,!!!!!vDX4W.Y1yVz_LQdmh0R|>,'
    '1<|box;x1=65;y1=63;x2=217;y2=197;w=1024;h=1024;c=0.85|>'
    '<|mask;x=0;y=0;c=0.85;data=135,153,!!!!!uBdlxq|>\n'
)


class SamParsing(unittest.TestCase):
    def test_real_image_answer_shape(self) -> None:
        # as Meta actually sends it: a <0f> header and c= in the mask token
        records = sam.parse_records(REAL_ANSWER)
        self.assertEqual([r["id"] for r in records], ["0", "1"])
        self.assertAlmostEqual(records[0]["conf"], 0.8439, places=3)
        self.assertEqual(records[0]["mask"]["height"], 140)
        self.assertEqual(records[0]["mask"]["width"], 146)

    def test_image_answer(self) -> None:
        records = sam.parse_records(IMAGE_ANSWER)
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first["id"], "7")
        self.assertEqual(first["box"], [100.0, 50.0, 300.0, 250.0])
        self.assertAlmostEqual(first["conf"], 0.92)
        # data is H,W (height first), marker selects the encoding
        self.assertEqual(first["mask"]["height"], 200)
        self.assertEqual(first["mask"]["width"], 200)
        self.assertEqual(first["mask"]["encoding"], "lossless")
        self.assertEqual(first["mask"]["payload"], "abcDEF")
        self.assertEqual(records[1]["mask"]["encoding"], "one_bit")
        kept = [r for r in records if r["conf"] >= 0.4]  # the threshold filter
        self.assertEqual([r["id"] for r in kept], ["7"])

    def test_video_answer_frames(self) -> None:
        records = sam.parse_records(VIDEO_ANSWER, video=True)
        self.assertEqual([(r["frame"], r["id"]) for r in records],
                         [(0, "3"), (5, "3")])

    def test_garbled_answer_raises(self) -> None:
        with self.assertRaises(sam.SamError):
            sam.parse_records("the orange is on the left, probably")


class MaskFit(unittest.TestCase):
    def test_big_object_mask_resized_to_box(self) -> None:
        mask = np.ones((256, 512), dtype=np.uint8)  # SAM scaled it down
        fitted = sam.fit_mask(mask, 1100, 560)
        self.assertEqual(fitted.shape, (560, 1100))

    def test_mask_within_2px_left_alone(self) -> None:
        mask = np.ones((100, 100), dtype=np.uint8)
        fitted = sam.fit_mask(mask, 101, 99)
        self.assertEqual(fitted.shape, (100, 100))

    def test_clipping_at_frame_edge(self) -> None:
        mask = np.ones((50, 50), dtype=np.uint8)
        fitted, x0, y0, view = sam.placed_mask(mask, [980, 10, 1029, 59], 1000, 500)
        fx0, fy0, fx1, fy1, sub = view
        self.assertEqual((fx1, fy1), (1000, 60))
        centre, area = sam.mask_centroid_area(mask, [980, 10, 1029, 59], 1000, 500)
        self.assertGreater(centre[0], 0.98)
        self.assertGreater(area, 0)


def _frames(boxes, step=0.04):
    return [(i, box, round(i * step, 3)) for i, box in enumerate(boxes)]


class Motion(unittest.TestCase):
    def test_still(self) -> None:
        boxes = [[100, 100, 200, 200]] * 10
        motion = sam.compute_motion(_frames(boxes))
        self.assertFalse(motion["moved"])
        self.assertTrue(motion["back"])

    def test_widening_and_back(self) -> None:
        boxes = [[100, 100, 200, 220]] * 3 + [[76, 100, 224, 220]] * 3 \
            + [[100, 100, 200, 220]] * 3  # width 100 -> 148 (+48%), then back
        motion = sam.compute_motion(_frames(boxes))
        self.assertTrue(motion["moved"])
        self.assertAlmostEqual(motion["peak"]["wider"], 0.48, places=2)
        self.assertTrue(motion["back"])
        self.assertIsNotNone(motion["active"])

    def test_shift_counts(self) -> None:
        boxes = [[100, 100, 200, 200]] * 3 + [[160, 100, 260, 200]] * 3
        motion = sam.compute_motion(_frames(boxes))
        self.assertTrue(motion["moved"])

    def test_spread_keyframes_when_still(self) -> None:
        objects = [{"moved": False, "seen": [0.0, 2.0],
                    "peak": {"change": 0.0, "t": 0.0}}]
        picks = sam.choose_keyframes(objects, [i * 0.1 for i in range(21)])
        self.assertEqual(picks, [0, 10, 20])


class Words(unittest.TestCase):
    def test_describe_lines(self) -> None:
        found = {"orange": [
            {"n": 1, "score": 0.92, "center": [0.63, 0.21], "area": 0.002},
            {"n": 2, "score": 0.5, "center": [0.5, 0.5], "area": 0.01}]}
        text = grounding.describe(found, 2100, ["orange"])
        self.assertIn("Found 2 × 'orange'", text)
        self.assertIn("#1: top centre at x=0.63, y=0.21; covers 0.2% of the view; "
                      "confidence 0.92", text)
        self.assertIn("#2: dead centre", text)

    def test_describe_not_found(self) -> None:
        text = grounding.describe({"orange": []}, 2100, ["orange"])
        self.assertTrue(text.startswith("No 'orange' in view (searched in 2.1s)."))

    def test_multi_locate_offset(self) -> None:
        found = {
            "orange": [{"n": 1, "score": 0.9, "center": [0.60, 0.38], "area": 0.01}],
            "small robot rover": [
                {"n": 2, "score": 0.8, "center": [0.39, 0.30], "area": 0.02}],
        }
        text = grounding.describe(found, 1000, ["orange", "small robot rover"])
        self.assertIn("From #2 (small robot rover) to #1 (orange): 0.21 to the "
                      "right and 0.08 lower in the picture (dx=+0.21, dy=+0.08).",
                      text)

    def test_describe_track(self) -> None:
        res = {"duration": 8.0, "frames": 163, "keytimes": [1.5, 2.6, 3.9, 5.9],
               "job": "abc", "gif_of": 1,
               "objects": [{"n": 1, "concept": "robot", "seen": [0.0, 8.0],
                            "moved": True, "active": [2.0, 5.4],
                            "peak": {"t": 2.6, "change": 0.48, "wider": 0.48,
                                     "taller": 0.0},
                            "back": True}]}
        text = grounding.describe_track("robot", res)
        self.assertIn("Watched for 8.0 s (163 frames).", text)
        self.assertIn("MOVED between 2.0 and 5.4 s, most at 2.6 s "
                      "(its outline 48% wider); back to how it started.", text)
        still = grounding.describe_track("robot", {
            "duration": 4.0, "frames": 90, "keytimes": [0.0], "objects": [
                {"n": 1, "concept": "robot", "seen": [0.0, 4.0], "moved": False,
                 "active": None, "peak": {"t": 0.0, "wider": 0, "taller": 0},
                 "back": True}]})
        self.assertIn("stayed still.", still)
        none = grounding.describe_track("robot", {
            "duration": 4.0, "frames": 90, "keytimes": [], "objects": []})
        self.assertIn("No 'robot' was found in the clip.", none)


class LocateEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["META_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "META_API_KEY", None)
        self._call_sam = sam.call_sam
        self._decode = sam.DECODER.decode
        self.addCleanup(setattr, sam, "call_sam", self._call_sam)
        self.addCleanup(setattr, sam.DECODER, "decode", self._decode)

        async def fake_call(content, timeout):
            return IMAGE_ANSWER

        sam.call_sam = fake_call
        sam.DECODER.decode = lambda masks: [
            np.ones((200, 200), dtype=np.uint8) for _ in masks]

    def test_locate_numbers_and_centroids(self) -> None:
        from fastapi.testclient import TestClient

        buf = io.BytesIO()
        from PIL import Image
        Image.new("RGB", (1000, 500), (200, 200, 200)).save(buf, format="JPEG")
        client = TestClient(sam.app)
        resp = client.post("/locate", json={
            "image": base64.b64encode(buf.getvalue()).decode(),
            "concepts": ["orange"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        items = data["found"]["orange"]
        self.assertEqual(len(items), 1)  # 0.20 is under the threshold
        self.assertEqual(items[0]["n"], 1)
        self.assertEqual(items[0]["score"], 0.92)
        self.assertGreater(items[0]["area"], 0)
        self.assertTrue(data["overlay"])

    def test_no_concepts_is_400(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(sam.app)
        resp = client.post("/locate", json={"image": "", "concepts": []})
        self.assertEqual(resp.status_code, 400)


class HouseLocateFlow(unittest.TestCase):
    def test_locate_flow(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent))
        from test_say import ScriptedProvider, make_house

        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            man = {"protocol": 1, "name": "kijkie", "display_name": "Kijkie",
                   "description": "A camera.",
                   "actions": {"look": {"description": "Look.", "returns": "image",
                                        "params": {"type": "object",
                                                   "properties": {}}}},
                   "state": {}}
            house.manifests["kijkie"] = man

            buf = io.BytesIO()
            from PIL import Image
            Image.new("RGB", (32, 32), (220, 120, 40)).save(buf, format="JPEG")
            jpeg = base64.b64encode(buf.getvalue()).decode()

            async def fake_act(rid, action, args=None, timeout=20):
                return {"ok": True, "text": "what your camera sees", "data": "x",
                        "image": jpeg, "image_url": "/api/robots/kijkie/seen/1.jpg"}

            async def fake_segment(image, concepts):
                return {"found": {"orange": [{"n": 1, "score": 0.92,
                                              "center": [0.63, 0.21],
                                              "area": 0.002}]},
                        "overlay": jpeg, "ms": 2100}

            self.addCleanup(setattr, grounding, "segment", grounding.segment)
            grounding.segment = fake_segment
            house.act = fake_act
            result = asyncio.run(house.locate("kijkie", "orange"))
            self.assertTrue(result["ok"])
            self.assertIn("#1: top centre at x=0.63, y=0.21", result["text"])
            located = [e for e in house.events if e.get("kind") == "located"]
            self.assertEqual(len(located), 1)
            self.assertTrue((Path(tmp) / "seen" / "kijkie").exists())


@unittest.skipUnless(
    (Path(__file__).parent.parent / "services" / "sam" / "js"
     / "node_modules").exists(),
    "the Node mask decoder isn't installed here")
class NodeDecoder(unittest.TestCase):
    def test_encode_then_decode_roundtrip(self) -> None:
        js_dir = Path(__file__).parent.parent / "services" / "sam" / "js"
        script = (
            "import {encodeSegmentationMask} from "
            "'./node_modules/@meta-sam/parser/dist/mask-codec.js';"
            "const raster=new Uint8Array(100); raster[0]=1; raster[55]=1;"
            "const m=encodeSegmentationMask(raster,10,10);"
            "console.log(JSON.stringify(m));")
        out = subprocess.run(["node", "--input-type=module", "-e", script],
                             capture_output=True, text=True, cwd=js_dir, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        mask = json.loads(out.stdout)
        wire = mask["payload"]  # the wire payload starts with its marker
        rasters = sam.DECODER.decode([{
            "encoding": mask["encoding"],
            "payload": wire[1:],
            "marker": wire[0],
            "width": mask["width"], "height": mask["height"]}])
        self.assertEqual(int(rasters[0].sum()), 2)


if __name__ == "__main__":
    unittest.main()
