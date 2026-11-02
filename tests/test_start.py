"""tools/start.py: what it runs, and where it listens (no processes started)."""
from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_PATH = Path(__file__).resolve().parent.parent / "tools" / "start.py"
_spec = importlib.util.spec_from_file_location("ponki_start", _PATH)
start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(start)


def args(**kw):
    return argparse.Namespace(lan=kw.get("lan", False), workshop=kw.get("workshop", False))


def tags(plan):
    return [tag for tag, *_ in plan]


class Plan(unittest.TestCase):
    def setUp(self):
        exists = mock.patch.object(Path, "exists", lambda self: "voice" not in str(self)
                                   and "ears" not in str(self))
        exists.start()
        self.addCleanup(exists.stop)

    def test_the_house_stays_on_this_machine(self):
        plan = start.plan(args(), {"HOUSE_BODY_PORT": "8210"})
        self.assertEqual(tags(plan), ["hub", "finding", "workshop"])
        hub_env = plan[0][2]
        self.assertEqual(hub_env["HOUSE_HOST"], "127.0.0.1")
        self.assertNotIn("HOUSE_BODY_PORT", hub_env)

    def test_lan_opens_the_door_and_checks_the_token(self):
        hub_env = start.plan(args(lan=True), {})[0][2]
        self.assertEqual(hub_env["HOUSE_HOST"], "0.0.0.0")
        self.assertEqual(hub_env["HOUSE_BODY_PORT"], "8210")
        self.assertEqual(hub_env["PONKI_BODY_BIND"], "0.0.0.0")  # so a weak token stops it

    def test_services_without_a_venv_are_off_not_broken(self):
        plan = start.plan(args(), {"HOUSE_EARS_URL": "http://127.0.0.1:8203"})
        hub_env = plan[0][2]
        self.assertEqual(hub_env["HOUSE_VOICE_OPTIONAL"], "on")
        self.assertEqual(hub_env["HOUSE_EARS_URL"], "off")
        self.assertEqual(hub_env["HOUSE_SAM_URL"], "http://127.0.0.1:8201")

    def test_the_house_workshop_has_the_desk_and_finds_its_hub(self):
        plan = start.plan(args(), {"HOUSE_PORT": "8300", "HOUSE_HUB": "ws://192.0.2.7:8200"})
        ws_env = plan[-1][2]
        self.assertEqual(ws_env["HOUSE_HUB"], "ws://127.0.0.1:8300")
        self.assertEqual(ws_env["HOUSE_WORKSHOP_NAME"], "desk-workshop")  # Pip's record names it
        self.assertEqual(ws_env["HOUSE_DESK"], "on")

    def test_workshop_only(self):
        plan = start.plan(args(workshop=True), {"HOUSE_HUB": "ws://192.0.2.7:8210"})
        self.assertEqual(tags(plan), ["workshop"])
        self.assertEqual(plan[0][2]["HOUSE_HUB"], "ws://192.0.2.7:8210")
        self.assertNotIn("HOUSE_DESK", plan[0][2])


class Settings(unittest.TestCase):
    def test_the_environment_wins_over_env_file(self):
        with mock.patch.dict(start.os.environ, {"HOUSE_HUMAN": "From the shell"}, clear=True), \
                mock.patch("dotenv.dotenv_values", return_value={"HOUSE_HUMAN": "From .env",
                                                                 "HOUSE_TOKEN": "t" * 43}), \
                mock.patch.object(Path, "exists", return_value=True):
            env = start.settings()
        self.assertEqual(env["HOUSE_HUMAN"], "From the shell")
        self.assertEqual(env["HOUSE_TOKEN"], "t" * 43)


if __name__ == "__main__":
    unittest.main()
