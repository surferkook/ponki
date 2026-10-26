"""Registry, rename and remembrance tests (spec 02). No hardware, no models."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from hub.house import House
from hub.registry import Registry


class StubProvider:
    model = "muse-spark-1.3"
    name = "meta"


def make_house(tmp: str) -> House:
    root = Path(tmp)
    (root / "souls").mkdir(parents=True, exist_ok=True)
    manifests = {
        "eye": {"protocol": 1, "name": "eye", "display_name": "Eye",
                "description": "Eye is a camera.", "actions": {}, "state": {}},
        "buddy": {"protocol": 1, "name": "buddy", "display_name": "Buddy",
                  "description": "A desk buddy.", "actions": {}, "state": {}},
    }
    for rid, display in (("eye", "Eye"), ("buddy", "Buddy")):
        (root / "souls" / f"{rid}.json").write_text(json.dumps({
            "persona": f"You are {display}.", "born": 1.0,
            "journal": [{"text": "hello"}], "conversation": [], "news": [],
        }), encoding="utf-8")
    house = House(StubProvider(), "Darko", root)
    for rid, manifest in manifests.items():
        house.manifests[rid] = manifest
        soul = house.soul(rid)
        house.registry.adopt(rid, kind="robot",
                             display_name=soul.display_name or manifest["display_name"])
    return house


class FakeWS:
    def __init__(self) -> None:
        self.closed_code = None
        self.sent: list = []

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, payload) -> None:
        self.sent.append(payload)


class RegistryBasics(unittest.TestCase):
    def test_upsert_adopt_pop_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            reg = Registry(path)
            reg.upsert("keiki", display_name="Keiki", family="onvif", workshop="pi")
            reg.adopt("keiki", workshop="nuc", runner="workshop")
            rec = reg.get("keiki")
            self.assertEqual(rec["workshop"], "pi")  # adopt never overwrites
            self.assertEqual(rec["runner"], "workshop")
            fresh = Registry(path)
            self.assertEqual(fresh.get("keiki")["family"], "onvif")  # saved
            popped = reg.pop("keiki")
            self.assertIsNone(reg.get("keiki"))
            reg.restore(popped)
            self.assertEqual(reg.by_display_name("keiki"), "keiki")  # case-insensitive

    def test_upsert_ignores_unknown_and_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry(Path(tmp) / "registry.json")
            reg.upsert("eye", display_name="Eye", bogus="x")
            reg.upsert("eye", display_name=None, family="onvif")
            rec = reg.get("eye")
            self.assertEqual(rec["display_name"], "Eye")
            self.assertNotIn("bogus", rec)


class HouseRegistry(unittest.TestCase):
    def test_rename_through_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            asyncio.run(house.rename("eye", "Keiki"))
            self.assertEqual(house.registry.get("eye")["display_name"], "Keiki")
            self.assertEqual(house.manifests["eye"]["display_name"], "Keiki")
            self.assertEqual(house.resolve("Keiki"), "eye")
            self.assertIn("Keiki", house.soul("eye").persona)
            self.assertEqual(house.manifests["eye"]["description"], "Keiki is a camera.")
            news = " ".join(str(n) for n in house.soul("buddy").news)
            self.assertIn("new name: Keiki", news)

    def test_a_name_is_used_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            with self.assertRaises(ValueError):
                asyncio.run(house.rename("buddy", "eye"))

    def test_goodbye_and_back_keeps_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            house.registry.upsert("eye", family="onvif", workshop="pi", runner="workshop")
            entry = house.remove("eye")
            self.assertIsNone(house.registry.get("eye"))
            self.assertEqual(entry["workshop"], "pi")
            house.bring_back(entry["id"])
            rec = house.registry.get("eye")
            self.assertEqual(rec["family"], "onvif")
            self.assertEqual(rec["workshop"], "pi")
            self.assertEqual(len(house.soul("eye").data["journal"]), 1)

    def test_forget_for_good_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            house.remove("buddy", forget=True)
            self.assertIsNone(house.registry.get("buddy"))
            self.assertNotIn("buddy", house.manifests)
            self.assertFalse((Path(tmp) / "souls" / "buddy.json").exists())

    def test_bring_back_retracts_unheard_news(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            entry = house.remove("eye")
            self.assertTrue(any("has moved on." in str(n) for n in house.soul("buddy").news))
            house.bring_back(entry["id"])
            self.assertFalse(any("has moved on." in str(n) for n in house.soul("buddy").news))

    def test_reconnect_replaces_and_late_detach_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp)
            manifest = dict(house.manifests["eye"])

            async def run() -> None:
                first, second = FakeWS(), FakeWS()
                await house.attach(first, manifest)
                await house.attach(second, manifest)
                self.assertEqual(first.closed_code, 4000)
                house.detach("eye", first)  # late detach must not remove the new link
                self.assertIs(house.bodies["eye"].ws, second)

            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
