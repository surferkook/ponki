"""Photo privacy tests (spec 02): fingerprints, deletion, rotation, export."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

from hub import apilog
from hub.export import export_media
from hub.house import House
from hub.imaging import for_model
from test_registry import make_house  # discover puts tests/ on sys.path


def jpeg_b64(width: int = 2000, height: int = 1200, colour=(200, 80, 40)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class Shapes(unittest.TestCase):
    def test_both_shapes_and_forget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apilog.configure(Path(tmp))
            a, b = jpeg_b64(colour=(255, 0, 0)), jpeg_b64(colour=(0, 0, 255))
            a_model, b_model = for_model(a), for_model(b)
            conversation = [
                {"role": "user", "content": [{"type": "image", "source": {"data": a_model}}]},
                {"role": "user", "content": [{"type": "image_url",
                                              "image_url": {"url": f"data:image/jpeg;base64,{b_model}"}}]},
            ]
            fps = apilog.images_in(conversation)
            self.assertEqual(fps, [apilog.fingerprint(a_model), apilog.fingerprint(b_model)])
            count = apilog.forget_image(conversation, fps[0], "[gone]")
            self.assertEqual(count, 1)
            self.assertEqual(apilog.images_in(conversation), [fps[1]])
            self.assertIn("[gone]", json.dumps(conversation))

    def test_for_model_small_image_unchanged(self) -> None:
        small = jpeg_b64(10, 10)
        self.assertIs(for_model(small), small)

    def test_log_names_photos_never_stores_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            apilog.configure(Path(tmp))
            model_copy = for_model(jpeg_b64())
            apilog.register(model_copy, "eye #7")
            message = {"role": "user",
                       "content": [{"type": "image", "source": {"data": model_copy}}]}
            apilog.model_call("eye", "muse-spark-1.3", [message], [message, message],
                              reply="ok", usage={"input_tokens": 3, "output_tokens": 1})
            lines = (Path(tmp) / "api_log.jsonl").read_text(encoding="utf-8").splitlines()
            entry = json.loads(lines[-1])
            self.assertNotIn(model_copy[:40], lines[-1])
            self.assertEqual(entry["photos_new"], ["eye #7"])
            self.assertEqual(entry["photos_in_request"], 2)


class Deletion(unittest.TestCase):
    def test_deleting_a_photo_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            url = house.store_photo("eye", jpeg_b64(64, 48))
            self.assertTrue(url.endswith("/1.jpg"))
            house.emit("saw", "eye", image=url)
            raw = (Path(tmp) / "seen" / "eye" / "1.jpg").read_bytes()
            model_copy = for_model(base64.b64encode(raw).decode("ascii"))
            eye = house.soul("eye")  # live soul, content-block shape
            eye.data["conversation"] = [
                {"content": [{"type": "image", "source": {"data": model_copy}}]}]
            eye.save()
            buddy_path = Path(tmp) / "souls" / "buddy.json"  # file only, OpenAI shape
            buddy = json.loads(buddy_path.read_text(encoding="utf-8"))
            buddy["conversation"] = [{"content": [{"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{model_copy}"}}]}]
            buddy_path.write_text(json.dumps(buddy), encoding="utf-8")
            house.souls.pop("buddy", None)

            result = house.delete_photo("eye", 1)
            self.assertEqual(result["conversations"], 2)
            self.assertFalse((Path(tmp) / "seen" / "eye" / "1.jpg").exists())
            self.assertEqual(apilog.images_in(eye.data["conversation"]), [])
            buddy_after = json.loads(buddy_path.read_text(encoding="utf-8"))
            self.assertEqual(apilog.images_in(buddy_after["conversation"]), [])
            saw = [e for e in house.events if e.get("kind") == "saw"][-1]
            self.assertIsNone(saw["image"])
            self.assertTrue(saw["photo_deleted"])
            with self.assertRaises(KeyError):
                house.delete_photo("eye", 1)

    def test_carried_photo_outlives_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            house.seen_keep = 2
            house.store_photo("eye", jpeg_b64(64, 48, (255, 0, 0)))
            raw = (Path(tmp) / "seen" / "eye" / "1.jpg").read_bytes()
            model_copy = for_model(base64.b64encode(raw).decode("ascii"))
            eye = house.soul("eye")
            eye.data["conversation"] = [
                {"content": [{"type": "image", "source": {"data": model_copy}}]}]
            eye.save()
            for i in range(3):
                house.store_photo("eye", jpeg_b64(64, 48, (0, i * 40, 0)))
            self.assertTrue((Path(tmp) / "seen" / "eye" / "1.jpg").exists())
            self.assertFalse((Path(tmp) / "seen" / "eye" / "2.jpg").exists())

    def test_numbers_are_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            house.store_photo("eye", jpeg_b64(32, 32))
            url2 = house.store_photo("eye", jpeg_b64(32, 32, (0, 255, 0)))
            house.delete_photo("eye", 2)
            rebuilt = House(house.provider, "Darko", Path(tmp))
            url3 = rebuilt.store_photo("eye", jpeg_b64(32, 32, (0, 0, 255)))
            self.assertTrue(url2.endswith("/2.jpg"))
            self.assertTrue(url3.endswith("/3.jpg"))


class Export(unittest.TestCase):
    def test_export_copies_and_lists_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            url = house.store_photo("eye", jpeg_b64(32, 32))
            import time

            house.emit("saw", "eye", image=url)
            house.emit("saw", "eye", image="/api/robots/eye/seen/99.jpg")
            out = Path(tmp) / "take"
            result = export_media(Path(tmp), time.time() - 60, None, None, out)
            self.assertEqual(result["copied"], 1)
            self.assertTrue(any("99" in name for name in result["missing"]))
            self.assertTrue((out / "index.json").exists())
            self.assertTrue((out / "feed.txt").exists())


if __name__ == "__main__":
    unittest.main()
