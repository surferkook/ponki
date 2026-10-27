"""Workshop tests (spec 09): exactly one supervisor may ever run.

Two supervisors race: both spawn the same robot, and the duplicate
drivers then fight over the robot's hub link (welcome/4000 flapping).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "bodies"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

os.environ.setdefault("HOUSE_TOKEN", "test")

from workshop import workshop  # noqa: E402


class OneSupervisor(unittest.TestCase):
    def test_main_starts_exactly_one_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            robots = Path(tmp) / "robots"
            robots.mkdir()
            (robots / "blinky.json").write_text(json.dumps(
                {"name": "blinky", "family": "pins"}), encoding="utf-8")
            spawns: list[str] = []

            def fake_run(self, *background):
                async def go():
                    task = asyncio.ensure_future(background[0])
                    await asyncio.sleep(1.2)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                asyncio.run(go())

            env = {"HOUSE_WORKSHOP_ROBOTS": str(robots),
                   "HOUSE_WORKSHOP_FAMILIES": "pins"}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(workshop, "_spawn",
                                      lambda name: spawns.append(name)), \
                    mock.patch.object(workshop.Body, "run", fake_run):
                before = {t.ident for t in threading.enumerate()}
                workshop.main()
                supervisors = [t for t in threading.enumerate()
                               if t.ident not in before
                               and getattr(t, "_target", None) is workshop.supervise]
            self.assertEqual(len(supervisors), 1,
                             "the workshop must run exactly one supervisor")
            self.assertEqual(spawns, ["blinky"],
                             "one supervisor spawns each robot once at start")


if __name__ == "__main__":
    unittest.main()
