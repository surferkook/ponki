"""The virtual desk: its family, its shared world and the camera's photos."""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bodies"))
sys.path.insert(0, str(ROOT / "bodies" / "desk"))

import families  # noqa: E402
from families import conformance  # noqa: E402

import world  # noqa: E402

desk = families.get("desk")


def _pixel(jpeg: bytes, xy: tuple[int, int]) -> tuple[int, int, int]:
    from PIL import Image

    return Image.open(io.BytesIO(jpeg)).convert("RGB").getpixel(xy)


class DeskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("HOUSE_DESK", "HOUSE_DESK_DIR")}
        os.environ["HOUSE_DESK_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_only_a_desk_workshop_finds_it(self) -> None:
        os.environ.pop("HOUSE_DESK", None)
        self.assertEqual(desk.discover({}), [])
        os.environ["HOUSE_DESK"] = "on"
        found = {d["id"]: d for d in desk.discover({"desk:camera": "pip"})}
        self.assertEqual(set(found), {"desk:camera", "desk:lamp"})
        self.assertFalse(found["desk:camera"]["ready"])
        self.assertEqual(found["desk:camera"]["robot"], "pip")
        self.assertTrue(found["desk:lamp"]["ready"])

    def test_conformance_live(self) -> None:
        os.environ["HOUSE_DESK"] = "on"
        self.assertEqual(conformance.check(desk, live=True), [])

    def test_world_is_shared_and_atomic(self) -> None:
        self.assertEqual(world.load()["lamp"]["color"], "off")
        world.update("lamp", color="blue")
        world.update("camera", pan=30)
        now = world.load()
        self.assertEqual(now["lamp"]["color"], "blue")
        self.assertEqual(now["camera"]["pan"], 30)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

    def test_the_lamp_shows_in_the_camera(self) -> None:
        # the mouth of the shade, seen from the right of the desk
        spot = (383, 492)
        off = _pixel(world.view(40), spot)
        world.update("lamp", color="red")
        red = _pixel(world.view(40), spot)
        self.assertGreater(red[0] - red[2], off[0] - off[2] + 60)

    def test_pan_is_clamped(self) -> None:
        self.assertEqual(world.view(500), world.view(world.PAN_LIMIT))

    def test_checks_return_pictures(self) -> None:
        device = desk.EXAMPLE["device"]
        config = desk.setup({}, device)
        self.assertEqual(list(desk.checks(config, device)), ["light", "nod"])
        result = asyncio.run(desk.check(device, config, "light"))
        self.assertTrue(result["image"].startswith(b"\xff\xd8"))
        # a check shows the change without making it
        self.assertEqual(world.load()["lamp"]["color"], "off")
        camera = {**device, "id": "desk:camera", "part": "camera"}
        config = desk.setup({}, camera)
        self.assertEqual(list(desk.checks(config, camera)), ["photo", "turn"])

    def test_setup_and_command(self) -> None:
        config = desk.setup({}, {"id": "desk:camera", "part": "camera", "status": "x"})
        self.assertEqual(config["part"], "camera")
        self.assertNotIn("status", config["device"])
        argv, env = desk.command("/tmp/pip.json", config)
        self.assertTrue(argv[-2].endswith("desk_body.py"))
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
