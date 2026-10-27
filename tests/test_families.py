"""Family contract tests (spec 09): conformance, guides, simulated build.

No hardware, no network: the only check hooks run here are the pins
family's on the simulated board.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bodies"))

import families  # noqa: E402
from families import conformance  # noqa: E402


def _mic_copy():
    """A mutable copy of the mic family module."""
    import families.mic as mic

    clone = types.SimpleNamespace()
    for key, value in vars(mic).items():
        if key.startswith("__"):
            continue
        setattr(clone, key, copy.deepcopy(value) if key == "EXAMPLE" else value)
    return clone


class ConformanceTests(unittest.TestCase):
    def test_every_family_passes(self) -> None:
        for name in families.NAMES:
            with self.subTest(family=name):
                self.assertEqual(conformance.check(name), [])

    def test_guides_name_every_family(self) -> None:
        text = families.guides()
        for name in families.NAMES:
            with self.subTest(family=name):
                self.assertIn(f"(family: {name})", text)


class CheckerCatchesBreakage(unittest.TestCase):
    """One break at a time on a copy of mic; each must be reported."""

    def _problems(self, clone) -> str:
        return "\n".join(conformance.check(clone))

    def test_missing_command_hook(self) -> None:
        clone = _mic_copy()
        del clone.command
        self.assertIn("missing hook command", self._problems(clone))

    def test_checks_wrong_signature(self) -> None:
        clone = _mic_copy()

        def checks(config):  # missing device
            return {}

        clone.checks = checks
        self.assertIn("hook checks", self._problems(clone))

    def test_bad_name(self) -> None:
        clone = _mic_copy()
        clone.NAME = "BAD NAME!"
        self.assertIn("NAME must match", self._problems(clone))

    def test_empty_scan(self) -> None:
        clone = _mic_copy()
        clone.SCAN = ""
        self.assertIn("SCAN must be a non-empty", self._problems(clone))

    def test_no_example(self) -> None:
        clone = _mic_copy()
        del clone.EXAMPLE
        self.assertIn("missing EXAMPLE", self._problems(clone))

    def test_checks_without_steps(self) -> None:
        clone = _mic_copy()

        def checks(config, device):
            return {"hearing": {"title": "hearing", "sig": "hearing"}}

        clone.checks = checks
        self.assertIn("steps must be a list", self._problems(clone))

    def test_command_env_non_string_value(self) -> None:
        clone = _mic_copy()

        def command(config_path, config):
            return ([sys.executable, "-u", "driver.py"], {"LEVEL": 3})

        clone.command = command
        self.assertIn("command env must be a dict of str to str",
                      self._problems(clone))

    def test_plan_that_raises_is_reported_as_crashed(self) -> None:
        clone = _mic_copy()

        def plan(config, args):
            raise RuntimeError("boom-plan")

        clone.plan = plan
        joined = self._problems(clone)
        self.assertIn("crashed", joined)
        self.assertIn("boom-plan", joined)


class SimulatedArduinoBuild(unittest.TestCase):
    """The pins build flow on the one simulated board.

    HOUSE_WORKSHOP_SIM=1 is what the operator sets; the workshop turns
    it into HOUSE_SIMULATED_BOARDS for the family (scan_devices).
    """

    def _discover(self, taken=None):
        import families.pins as pins

        env = {"HOUSE_WORKSHOP_SIM": "1", "HOUSE_SIMULATED_BOARDS": "1"}
        with mock.patch.dict(os.environ, env):
            return pins.discover(taken or {})

    def _sim_board(self):
        boards = [d for d in self._discover()
                  if str(d.get("id", "")).startswith("sim")]
        self.assertEqual(len(boards), 1)
        return boards[0]

    def test_discover_finds_exactly_one_simulated_board(self) -> None:
        board = self._sim_board()
        self.assertEqual(board["id"], "sim")
        self.assertTrue(board["ready"])

    def test_connect_makes_ready(self) -> None:
        import families.pins as pins

        board = dict(self._sim_board(), ready=False)
        _text, updated = pins.connect(board, {})
        self.assertTrue(updated["ready"])

    def test_setup_plan_checks_and_check_hooks(self) -> None:
        import families.pins as pins

        board = self._sim_board()
        _text, board = pins.connect(board, {})
        self.assertTrue(board["ready"])

        config: dict = {"family": "pins", "device": dict(board)}
        config = pins.setup(config, board)
        plan_data = copy.deepcopy(pins.EXAMPLE["plan"])
        # The builder keeps the plan's name on the config (as conformance
        # does); parts are the example's plus a servo.
        config["name"] = plan_data["name"]
        parts = list(plan_data["parts"]) + [
            {"id": "arm", "kind": "servo", "about": "your flag arm"}]
        pins.plan(config, {"parts": parts})

        problems, _warnings = pins.problems(config)
        self.assertEqual(problems, [])

        checks = pins.checks(config, board)
        self.assertEqual(set(checks), {"lamp", "arm"})
        for entry in checks.values():
            self.assertTrue(entry["steps"])
            self.assertTrue(all(isinstance(s, str) for s in entry["steps"]))

        # Simulated board only: no hardware behind these hooks.
        for check_id in checks:
            result = asyncio.run(pins.check(board, config, check_id))
            self.assertIsInstance(result["expect"], str)
            self.assertTrue(result["expect"])

    def test_board_in_taken_reports_robot_and_not_ready(self) -> None:
        boards = self._discover({"sim": "blinky-robot"})
        sims = [d for d in boards if d.get("id") == "sim"]
        self.assertEqual(len(sims), 1)
        self.assertFalse(sims[0]["ready"])
        self.assertEqual(sims[0].get("robot"), "blinky-robot")


if __name__ == "__main__":
    unittest.main()
