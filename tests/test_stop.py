"""A person's "stop" halts the whole house, not just the robot it was said to: a guide waiting on a rover
mid-drive used to stop only after the rover had driven off the table. And a robot's finder words reach its
housemates, so a guide doesn't burn its turn guessing what to search for.

    .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from hub import house as house_mod  # noqa: E402
from hub.providers import Step  # noqa: E402
from test_say import ScriptedProvider, call, make_house, no_speak  # noqa: E402


class WhatCountsAsStop(unittest.TestCase):
    def test_short_stops_in_any_shape(self):
        for text in ("stop", "Guys, stop.", "guys stop guys", "Keiki, stop!", "Stoppen!",
                     "Vroom-Vroom halt", "Keiki, stop guiding him"):
            self.assertTrue(house_mod.is_stop(text), text)

    def test_a_sentence_that_mentions_stop_is_not_one(self):
        for text in ("Keiki, where's the orange?",
                     "can you drive to the bus stop and then come back here please",
                     "", "stopwatch"):
            self.assertFalse(house_mod.is_stop(text), text)


class StopEverything(unittest.TestCase):
    def test_every_body_that_can_stop_stops_and_busy_souls_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            buddy = dict(house.manifests["buddy"])   # a copy: the test manifests are shared
            buddy["actions"] = {**buddy["actions"], "stop": {"description": "Stop the wheels now."}}
            house.manifests["buddy"] = buddy
            for rid in ("eye", "buddy"):
                house.bodies[rid] = house_mod.Link(ws=None, manifest=house.manifests[rid], state={})
            acted = []

            async def act(key, action, args=None, timeout=None):
                acted.append((key, action))
                return {"ok": True, "text": "stopped", "data": None}
            house.act = act

            async def run():
                eye = house.soul("eye")
                async with eye.lock:          # eye is mid-turn (waiting on an ask)
                    await house.stop_everything()
                    return eye._interrupt
            self.assertTrue(asyncio.run(run()))
            self.assertEqual(acted, [("buddy", "stop")])

    def test_told_to_stop_while_thinking_its_next_action_never_happens(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            no_speak(house)
            acted = []

            async def act(key, action, args=None, timeout=None):
                acted.append(action)
                return {"ok": True, "text": "done", "data": "done"}
            house.act = act
            provider.scripts["buddy"] = [call("face", expression="happy"), Step(text="Done!")]
            # "stop" arrives while the brain is still deciding on the face
            provider.step_hooks["buddy"] = lambda: house.soul("buddy").request_interrupt(
                "a person said stop")
            reply = asyncio.run(house.soul("buddy").turn("make a happy face", "human"))
            self.assertEqual(acted, [])
            self.assertEqual(reply, "")
            thoughts = [e["text"] for e in house.events if e.get("kind") == "thought"]
            self.assertIn("(stopped — a person said stop)", thoughts)


class FinderWords(unittest.TestCase):
    def test_housemates_are_told_what_to_search_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            house.soul("buddy").set_looks("  small   blue robot car ")
            self.assertIn("To a camera's finder it's 'small blue robot car'.", house.view_for("eye"))
            self.assertNotIn("finder", house.view_for("buddy"))


class OneMomentOneCheer(unittest.TestCase):
    """Seen live: Buddy's "celebrate when a housemate finishes a task" fired three times for one arrival,
    once for each line about it (the guide's announcement, the rover's hooray, the guide's woohoo)."""

    def test_the_lines_after_an_arrival_are_the_same_moment(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            for rid in ("eye", "buddy"):
                house.bodies[rid] = house_mod.Link(ws=object(), manifest=house.manifests[rid], state={})
            no_speak(house)
            house.soul("buddy").promises.append({"text": "when a housemate finishes a task, celebrate"})
            provider.floor_handler = lambda request: '{"react": null, "also": ["buddy"], "why": "done"}'

            async def run():
                await house._run_floor("eye", "Vroom made it beside the orange!", ["buddy"], False, 0, "human")
                await house._run_floor("eye", "Woohoo, great driving!", ["buddy"], False, 0, "human")
                await asyncio.sleep(0.1)
            asyncio.run(run())
            kept = [e for e in house.events if e.get("kind") == "overheard" and e.get("promise")]
            self.assertEqual(len(kept), 1)


def spot(**concepts):
    """found{} the way SAM returns it: concept → [(x, y), ...]."""
    return {c: [{"n": i + 1, "center": list(pt), "area": 0.01, "score": 0.9}
                for i, pt in enumerate(pts)] for c, pts in concepts.items()}


class WhichWayItFaces(unittest.TestCase):
    """Seen live: Vroom-Vroom ended a move with "turn 90° right, roll 25" beside the orange, and Keiki,
    still picturing him facing up the table, sent him "150° to your left" off the far edge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.house = make_house(self.tmp.name, ScriptedProvider())
        self.house.registry.adopt("rover", kind="robot", display_name="Vroom-Vroom")

    def tearDown(self):
        self.tmp.cleanup()

    def moved(self, t, *moves):
        self.house._moves["rover"] = [(t, kind, amount) for kind, amount in moves]

    def test_known_by_finder_words_it_faces_the_way_it_drove(self):
        self.house.soul("rover").set_looks("car")
        first = self.house._facing_note("eye", spot(car=[(0.461, 0.173)], orange=[(0.6, 0.18)]), 100.0)
        self.assertIn("isn't known yet", first)      # never guess a facing from a still photo
        self.moved(150.0, ("turn", -90.0), ("drive", 25.0))      # the turn came before the drive
        note = self.house._facing_note("eye", spot(car=[(0.59, 0.202)], orange=[(0.6, 0.18)]), 200.0)
        self.assertIn("now faces to the right of your view", note)
        self.assertIn("its right is toward you", note)

    def test_new_words_it_is_the_one_thing_that_moved(self):
        before = spot(robot=[(0.784, 0.835), (0.431, 0.903)], orange=[(0.602, 0.176)])
        self.house._facing_note("eye", before, 100.0)
        self.moved(150.0, ("drive", 80.0))
        note = self.house._facing_note(
            "eye", spot(car=[(0.343, 0.285)], orange=[(0.602, 0.176)], robot=[(0.783, 0.836)]), 200.0)
        self.assertIn("from x=0.43, y=0.90 to x=0.34, y=0.28", note)
        self.assertIn("now faces away from you", note)

    def test_a_turn_after_the_drive_is_counted(self):
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.44, 0.81)]), 100.0)
        self.moved(150.0, ("drive", 40.0), ("turn", -90.0))
        note = self.house._facing_note("eye", spot(car=[(0.44, 0.60)]), 200.0)
        self.assertIn("turned 90° to its right", note)
        self.assertIn("now faces to the right of your view", note)

    def test_its_facing_is_kept_across_a_missed_look_and_a_new_task(self):
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.46, 0.17)], orange=[(0.6, 0.18)]), 100.0)
        self.moved(150.0, ("turn", -90.0), ("drive", 25.0))
        self.house._facing_note("eye", spot(orange=[(0.6, 0.18)]), 180.0)      # the finder missed it
        self.house._facing_note("eye", spot(car=[(0.59, 0.20)]), 200.0)        # it drove right
        note = self.house._facing_note("eye", spot(car=[(0.59, 0.20)], mug=[(0.13, 0.68)]), 300.0)
        self.assertIn("hasn't driven since", note)
        self.assertIn("its right is toward you", note)

    def test_it_says_the_turn_toward_the_goal(self):
        """Live: facing away-right at the far end, the mug almost straight behind; Keiki guessed 150° left
        (needed ~170°) and the 20° miss took him over the edge."""
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.40, 0.17)]), 100.0)
        self.moved(150.0, ("drive", 30.0))
        self.house._facing_note("eye", spot(car=[(0.484, 0.07)]), 200.0)
        note = self.house._facing_note("eye", spot(car=[(0.484, 0.206)], mug=[(0.138, 0.593)]), 300.0)
        turn = int(note.split("would turn about ")[1].split("°")[0])
        self.assertIn("° to its left", note)
        self.assertTrue(160 <= turn <= 180, note)

    def test_a_camera_that_turns_its_head_forgets_where_things_were(self):
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.44, 0.81)]), 100.0)
        self.house._sightings["eye"]["robots"]["rover"]["at"] = None   # what act() does on a head turn
        self.moved(150.0, ("drive", 40.0))
        note = self.house._facing_note("eye", spot(car=[(0.10, 0.20)]), 200.0)
        self.assertNotIn("drove from", note)
        self.assertIn("isn't known yet", note)

    def test_a_stop_forgets_every_facing(self):
        """A stop usually means a hand caught the robot: live, the 'drive' after a catch was the hand."""
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.44, 0.81)]), 100.0)
        self.moved(150.0, ("drive", 40.0))
        self.assertIn("now faces", self.house._facing_note("eye", spot(car=[(0.44, 0.60)]), 200.0))
        asyncio.run(self.house.stop_everything())
        self.assertIn("isn't known yet", self.house._facing_note("eye", spot(car=[(0.44, 0.60)]), 300.0))

    def test_a_short_drive_gives_only_a_rough_facing(self):
        """Live: a 10 cm probe at the far end, then 80 cm on its heading, into the Pi's cables."""
        self.house.soul("rover").set_looks("car")
        self.house._facing_note("eye", spot(car=[(0.608, 0.141)]), 100.0)
        self.moved(150.0, ("drive", 10.0))
        note = self.house._facing_note("eye", spot(car=[(0.566, 0.143)], orange=[(0.455, 0.829)]), 200.0)
        self.assertIn("now faces to the left of your view", note)
        self.assertIn("about 40 centimetres", note)

    def test_nothing_drove_nothing_said(self):
        self.house._facing_note("eye", spot(car=[(0.44, 0.81)]), 100.0)
        self.assertEqual(self.house._facing_note("eye", spot(car=[(0.44, 0.81)]), 200.0), "")


if __name__ == "__main__":
    unittest.main()
