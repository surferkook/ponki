"""Standing in: a networked robot whose workshop has lost it moves to one that can reach
it, one robot keeps one body, and accounts pass workshop to workshop. No hardware."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))
os.environ.setdefault("HOUSE_TOKEN", "test")

from hub import house as house_mod  # noqa: E402
from hub.house import House, Link  # noqa: E402
from workshop import workshop  # noqa: E402

from tests.test_registry import FakeWS, StubProvider  # noqa: E402

KEIKI = {"name": "keiki", "display_name": "Keiki", "description": "A camera.",
         "family": "onvif", "device": {"id": "onvif:192.0.2.39", "host": "192.0.2.39"}}
WORKSHOP_ACTIONS = {a: {} for a in ("robots", "adopt_robot", "stop_robot", "can_run",
                                    "share_account", "take_account")}


def make_house(tmp: str) -> tuple[House, list]:
    house = House(StubProvider(), "Darko", Path(tmp))
    house.registry.upsert("keiki", kind="robot", display_name="Keiki", family="onvif",
                          workshop="pi", runner="workshop", device=KEIKI["device"])
    house.registry.upsert("buddy", kind="robot", display_name="Buddy", family="pins",
                          workshop="pi", runner="workshop", device={"id": "/dev/ttyACM0"})
    calls: list = []

    async def fake_act(key, action, args=None, timeout=20):
        calls.append((key, action, args or {}))
        if action == "can_run":
            return {"ok": True, "text": "", "data": {"ready": False, "missing": "account"}}
        if action == "share_account":
            return {"ok": True, "text": "", "data": {"username": "u", "password": "p"}}
        return {"ok": True, "text": "done", "data": "done"}

    house.act = fake_act
    return house, calls


def workshop_link(families: list[str]) -> Link:
    return Link(ws=FakeWS(), manifest={"name": "nuc", "kind": "workshop", "actions": WORKSHOP_ACTIONS},
                state={"families": families})


class Takeover(unittest.TestCase):
    def test_a_networked_robot_moves_once_its_workshop_has_been_gone_a_while(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, calls = make_house(tmp)
            house.fixtures["nuc"] = workshop_link(["robohon", "onvif"])
            asyncio.run(house.check_takeovers(now=1000.0))
            self.assertEqual(calls, [])  # just noticed: give it its minute
            asyncio.run(house.check_takeovers(now=1000.0 + house_mod.TAKEOVER_AFTER + 1))
            adopted = [c for c in calls if c[1] == "adopt_robot"]
            self.assertEqual([c[0] for c in adopted], ["@nuc"])
            self.assertEqual(adopted[0][2]["config"]["name"], "keiki")
            record = house.registry.get("keiki")
            self.assertEqual((record["workshop"], record["moved_from"]), ("nuc", "pi"))
            moved = [e for e in house.events if e["kind"] == "moved"]
            self.assertIn("left the house", moved[-1]["text"])
            # Buddy is on a cable to the Pi: no workshop here runs his family, so he waits
            self.assertEqual(house.registry.get("buddy")["workshop"], "pi")

    def test_a_workshop_that_is_here_but_lost_it_sets_its_copy_aside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, calls = make_house(tmp)
            house.fixtures["nuc"] = workshop_link(["onvif"])
            house.fixtures["pi"] = Link(ws=FakeWS(), manifest={"name": "pi", "kind": "workshop",
                                                               "actions": WORKSHOP_ACTIONS},
                                        state={"families": ["onvif", "pins"]})
            asyncio.run(house.check_takeovers(now=0.0))
            asyncio.run(house.check_takeovers(now=house_mod.TAKEOVER_AFTER + 1))
            self.assertIn(("@pi", "stop_robot", {"name": "keiki", "forget": True}), calls)
            moved = [e for e in house.events if e["kind"] == "moved"]
            self.assertIn("couldn't reach it", moved[-1]["text"])

    def test_nobody_able_to_stand_in_is_said_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, calls = make_house(tmp)
            house.fixtures["nuc"] = workshop_link(["onvif"])

            async def refuse(key, action, args=None, timeout=20):
                return {"ok": False, "text": "keiki's account isn't on this workshop", "data": None}

            house.act = refuse
            for now in (0.0, 61.0, 61.0 + house_mod.TAKEOVER_RETRY + 1):
                asyncio.run(house.check_takeovers(now=now))
            stuck = [e for e in house.events if e["kind"] == "stuck"]
            self.assertEqual(len(stuck), 1)
            self.assertIn("account isn't on this workshop", stuck[0]["text"])
            self.assertEqual(house.registry.get("keiki")["workshop"], "pi")


class OneBody(unittest.TestCase):
    def test_an_old_copy_is_refused_and_the_new_one_welcomed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, _calls = make_house(tmp)
            house.registry.upsert("keiki", workshop="nuc", moved_from="pi")
            stale = FakeWS()
            with self.assertRaises(ValueError):
                asyncio.run(house.attach(stale, {"name": "keiki", "actions": {}}))
            self.assertEqual(stale.closed_code, 4009)
            self.assertNotIn("keiki", house.bodies)
            with mock.patch.object(House, "_birth", lambda self, rid, manifest: asyncio.sleep(0)):
                key = asyncio.run(house.attach(FakeWS(), {"name": "keiki", "actions": {},
                                                          "workshop": "nuc"}))
            self.assertEqual(key, "keiki")

    def test_the_workshop_it_left_sets_its_copy_aside_when_it_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, calls = make_house(tmp)
            house.registry.upsert("keiki", workshop="nuc", moved_from="pi")

            async def listing(key, action, args=None, timeout=20):
                calls.append((key, action, args or {}))
                if action == "robots":
                    return {"ok": True, "text": "", "data": [
                        {"name": "keiki", "family": "onvif", "kind": "robot", "reach": "network"}]}
                return {"ok": True, "text": "done", "data": "done"}

            house.act = listing
            real_sleep = asyncio.sleep
            with mock.patch.object(house_mod.asyncio, "sleep", lambda s: real_sleep(0)):
                asyncio.run(house._adopt_from_workshop("pi"))
            self.assertIn(("@pi", "stop_robot", {"name": "keiki", "forget": True}), calls)
            self.assertEqual(house.registry.get("keiki")["workshop"], "nuc")


class Accounts(unittest.TestCase):
    def test_a_stand_in_gets_the_account_from_the_owner_through_the_hub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house, calls = make_house(tmp)
            (Path(tmp) / "handover").mkdir()
            (Path(tmp) / "handover" / "keiki.json").write_text(json.dumps(KEIKI))
            house.fixtures["pi"] = Link(ws=FakeWS(), manifest={"name": "pi", "kind": "workshop",
                                                               "actions": WORKSHOP_ACTIONS},
                                        state={"families": ["onvif"]})
            house.fixtures["nuc"] = workshop_link(["onvif"])
            asyncio.run(house._share_accounts())
            self.assertIn(("@pi", "share_account", {"name": "keiki"}), calls)
            taken = [c for c in calls if c[1] == "take_account"]
            self.assertEqual(taken[0][0], "@nuc")
            self.assertEqual(taken[0][2]["secret"], {"username": "u", "password": "p"})
            # relayed, never kept: no account in the hub's files or its feed
            for path in Path(tmp).rglob("*.json*"):
                self.assertNotIn('"password"', path.read_text(encoding="utf-8"), path)
            self.assertNotIn("password", json.dumps(list(house.events)))


class WorkshopSide(unittest.TestCase):
    def test_a_workshop_stands_in_only_with_the_family_the_account_and_the_reach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOUSE_WORKSHOP_ROBOTS": str(Path(tmp) / "robots"),
                   "HOUSE_WORKSHOP_FAMILIES": "robohon,onvif"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(workshop.reach_of("onvif"), "network")
                self.assertEqual(workshop.reach_of("pins"), "attached")
                missing, _ = workshop.ready_to_stand_in({**KEIKI, "family": "pins"})
                self.assertEqual(missing, "family")
                missing, why = workshop.ready_to_stand_in(KEIKI)
                self.assertEqual(missing, "account")
                body = workshop.build_body()
                asyncio.run(body._handlers["take_account"](
                    config=KEIKI, secret={"username": "u", "password": "p"}))
                secret = Path(tmp) / "secrets" / "onvif:192.0.2.39.json"
                self.assertEqual(oct(secret.stat().st_mode & 0o777), "0o600")
                with mock.patch("socket.create_connection", side_effect=OSError("no route")):
                    missing, why = workshop.ready_to_stand_in(KEIKI)
                self.assertEqual(missing, "reach")
                self.assertIn("can't reach the camera", why)
                with mock.patch("socket.create_connection") as conn:
                    self.assertEqual(workshop.ready_to_stand_in(KEIKI), ("", ""))
                    conn.return_value.close.assert_called_once()


class EventLog(unittest.TestCase):
    def test_deleting_a_photo_keeps_every_line_of_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [{"t": i, "kind": "say", "robot": "keiki", "text": f"line {i}"}
                     for i in range(house_mod.EVENTS_KEEP + 500)]
            lines.append({"t": 9999, "kind": "saw", "robot": "keiki",
                          "image": "/api/robots/keiki/seen/7.jpg"})
            (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines))
            house = House(StubProvider(), "Darko", root)
            (root / "seen" / "keiki").mkdir(parents=True)
            from PIL import Image
            Image.new("RGB", (8, 8)).save(root / "seen" / "keiki" / "7.jpg")
            house.delete_photo("keiki", 7)
            kept = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()]
            self.assertEqual(kept[0]["text"], "line 0")
            self.assertGreaterEqual(len(kept), len(lines))
            saw = [e for e in kept if e["kind"] == "saw"][0]
            self.assertEqual((saw["image"], saw["photo_deleted"]), (None, True))
            House(StubProvider(), "Darko", root)  # and a restart trims nothing either
            self.assertEqual(len((root / "events.jsonl").read_text().splitlines()), len(kept))


if __name__ == "__main__":
    unittest.main()
