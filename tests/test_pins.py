"""Pins tests (spec 04): the library and the driver on the simulated board."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bodies" / "pins"))
sys.path.insert(0, str(ROOT / "bodies" / "families"))

import parts as parts_lib  # noqa: E402
import pins_body  # noqa: E402
import pins as pins_family  # noqa: E402

BUDDY = json.loads((ROOT / "bodies" / "pins" / "buddy.json").read_text())


class Validate(unittest.TestCase):
    def test_buddy_config_one_warning_no_problems(self) -> None:
        problems, warnings = parts_lib.validate(copy.deepcopy(BUDDY))
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("D3", warnings[0])  # mood red on 3, with a piezo

    def test_dimmer_on_9_with_servo_is_a_problem(self) -> None:
        config = {"name": "x", "board": "uno", "parts": {
            "arm": {"kind": "servo", "pins": {"signal": 10}},
            "lamp": {"kind": "rgb_led", "pins": {"r": 9, "g": 5, "b": 6}}}}
        problems, _ = parts_lib.validate(config)
        self.assertTrue(any("D9" in p for p in problems))

    def test_pin_used_twice(self) -> None:
        config = {"name": "x", "board": "uno", "parts": {
            "a": {"kind": "led", "pins": {"signal": 7}},
            "b": {"kind": "led", "pins": {"signal": 7}}}}
        problems, _ = parts_lib.validate(config)
        self.assertTrue(any("already used by a.signal" in p for p in problems))

    def test_pwm_role_on_plain_pin_cant_dim(self) -> None:
        config = {"name": "x", "board": "uno", "parts": {
            "mood": {"kind": "rgb_led", "pins": {"r": 2, "g": 5, "b": 6}}}}
        problems, _ = parts_lib.validate(config)
        self.assertTrue(any("can't dim" in p for p in problems))

    def test_voice_must_be_a_piezo(self) -> None:
        config = copy.deepcopy(BUDDY)
        config["voice"] = "mood"
        problems, _ = parts_lib.validate(config)
        self.assertTrue(any("must be the id of a piezo" in p for p in problems))


class Assign(unittest.TestCase):
    def test_preferences(self) -> None:
        config = {"name": "x", "board": "uno", "parts": {
            "arm": {"kind": "servo"}, "beeper": {"kind": "piezo"},
            "mood": {"kind": "rgb_led"}, "light": {"kind": "light_sensor"}}}
        parts_lib.assign_pins(config)
        pins = {pid: p["pins"] for pid, p in config["parts"].items()}
        self.assertEqual(pins["arm"]["signal"], 9)
        self.assertEqual(pins["beeper"]["signal"], 8)
        self.assertEqual((pins["mood"]["r"], pins["mood"]["g"], pins["mood"]["b"]),
                         (5, 6, 3))
        self.assertEqual(pins["light"]["signal"], "A0")

    def test_replan_keeps_existing_pins(self) -> None:
        config = {"name": "x", "board": "uno", "parts": {
            "arm": {"kind": "servo", "pins": {"signal": 12}},
            "beeper": {"kind": "piezo"}}}
        parts_lib.assign_pins(config)
        self.assertEqual(config["parts"]["arm"]["pins"]["signal"], 12)


class Chirp(unittest.TestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(parts_lib.chirp_notes("Hello there, house!"),
                         parts_lib.chirp_notes("Hello there, house!"))

    def test_question_ends_higher_than_statement(self) -> None:
        def last_hz(notes):
            return [hz for hz, _ms in notes if hz > 0][-1]
        self.assertGreater(last_hz(parts_lib.chirp_notes("Are you awake?")),
                           last_hz(parts_lib.chirp_notes("You are awake.")))

    def test_sleepy_is_lower(self) -> None:
        def first_hz(notes):
            return [hz for hz, _ms in notes if hz > 0][0]
        self.assertLess(first_hz(parts_lib.chirp_notes("Goodnight, time for bed.")),
                        first_hz(parts_lib.chirp_notes("Good morning, wake up!")))

    def test_seconds_shortens(self) -> None:
        full = parts_lib.chirp_notes("A very long sentence with many many words in it.")
        short = parts_lib.chirp_notes("A very long sentence with many many words in it.",
                                      seconds=0.2)
        self.assertLess(sum(ms for _h, ms in short), sum(ms for _h, ms in full))


class SuddenLight(unittest.TestCase):
    def _sensor(self, told):
        class FakePins:
            def mode(self, pin, mode): pass
            def read(self, pin): return 0
        async def tell(text):
            told.append(text)
        board = parts_lib.BOARDS["uno"]
        return parts_lib.LightSensorPart(
            "light", {"kind": "light_sensor", "pins": {"signal": "A0"}}, board,
            FakePins(), tell)

    def test_sudden_rule(self) -> None:
        told: list = []
        sensor = self._sensor(told)

        async def run():
            await sensor.on_reading({"light_level": 300, "room_is": "dim"})
            await sensor.on_reading({"light_level": 140, "room_is": "dark"})
            await sensor.on_reading({"light_level": 900, "room_is": "bright"})
            await sensor.on_reading({"light_level": 600, "room_is": "bright"})
        asyncio.run(run())
        self.assertEqual(len(told), 1)  # 300→140 sudden; 900→600 is not
        self.assertIn("suddenly went dark", told[0])

    def test_small_move_is_not_sudden(self) -> None:
        told: list = []
        sensor = self._sensor(told)

        async def run():
            await sensor.on_reading({"light_level": 150, "room_is": "dark"})
            await sensor.on_reading({"light_level": 60, "room_is": "dark"})
        asyncio.run(run())
        self.assertEqual(told, [])  # moved less than 100


class DriverOnSim(unittest.TestCase):
    def test_manifest_and_actions(self) -> None:
        config = copy.deepcopy(BUDDY)
        sim = pins_body.SimBoard()
        body, part_objs = pins_body.build_body(config, sim)
        manifest = body.manifest()
        names = set(manifest["actions"])
        self.assertTrue({"mood", "arm", "arm_wave", "beeper_sound", "chirp",
                         "sense", "self_test"} <= names)
        self.assertTrue(manifest["actions"]["self_test"]["hidden"])
        self.assertEqual(manifest["voice"], "chirp")
        self.assertEqual(manifest["voice_style"], "chirps")

        sent: list = []
        async def capture(obj):
            sent.append(obj)
        body._send = capture

        async def run():
            for part in part_objs:
                await asyncio.to_thread(part.setup)
            await body._handle_act({"id": "1", "action": "mood",
                                    "args": {"color": "red"}})
            await pins_body.reading_round(body, part_objs, sim)
        asyncio.run(run())
        self.assertEqual(sim.values[3], 255)   # red leg on D3
        self.assertEqual(sim.values[6], 0)
        self.assertEqual(sim.values[5], 0)
        self.assertTrue(sent[0]["ok"])
        self.assertEqual(body.state.get("room"), "bright")  # sim light ~600


class Family(unittest.TestCase):
    def test_example_plan_assigns_pins_and_wiring(self) -> None:
        config = {"name": "blinky", "board": "uno", "parts": {}}
        text = pins_family.plan(config, pins_family.EXAMPLE["plan"])
        self.assertIn("lamp", config["parts"])
        self.assertIsNotNone(config["parts"]["lamp"]["pins"].get("signal"))
        self.assertIn("lamp", text)
        checks = pins_family.checks(config, pins_family.EXAMPLE["device"])
        self.assertEqual(list(checks), ["lamp"])
        self.assertTrue(checks["lamp"]["steps"])


if __name__ == "__main__":
    unittest.main()
