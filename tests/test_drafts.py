"""Draft family tests (spec 09): hub review + sandboxed workshop runs.

The pretend family is a USB lamp. Its good draft is two strings; broken
variants are one edit away. Bubblewrap tests are skipped when bwrap is
not installed and run on temp drafts/families folders.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bodies"))

from families import drafts  # noqa: E402

FAMILY_PY = '''import os
import subprocess  # noqa: F401  (shown as a reach, never a problem)

NAME = "testlamp"
TITLE = "Test lamp"
GUIDE = "A pretend USB lamp for tests: plug it in, connect, then run the glow check and look at the light."
MATCH = ["abcd:1234"]
CONNECT = {"label": "", "hint": "", "fields": []}
CHECKS = {"glow": {"title": "glow", "steps": ["Plug the lamp in.", "Look at the lamp."]}}
BODY = {"head": ["Part", "What it is"], "rows": [["lamp", "a pretend USB lamp"]]}
CHECK_HINT = ""
EXAMPLE = {"device": {"id": "usb:abcd:1234:TEST1", "name": "Test Lamp"},
           "plan": {"name": "testlamp", "description": "A pretend USB lamp.",
                    "personality": "bright and cheerful"}}


def connect(device, fields):
    seen = os.path.exists(os.path.expanduser("~/.ssh"))
    updated = dict(device)
    updated["ready"] = True
    updated["ssh_seen"] = seen
    return ("the lamp is connected", updated)


def check(device, config, check_id):
    if check_id != "glow":
        raise ValueError("unknown check")
    return {"expect": "the lamp glows", "image": b"\\xff\\xd8\\xff\\xe0testlamp"}
'''

DRIVER_PY = '''from body import Body

body = Body("testlamp", "A pretend USB lamp.")

if __name__ == "__main__":
    body.run()
'''

DEVICE = {"id": "usb:abcd:1234:TEST1", "name": "Test Lamp",
          "usb": "1-1", "nodes": []}


def _review(family_py=FAMILY_PY, driver_py=DRIVER_PY):
    return drafts.review({"family.py": family_py, "driver.py": driver_py})


class ReviewTests(unittest.TestCase):
    def test_good_draft_reviews_clean(self) -> None:
        result = _review()
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["meta"]["NAME"], "testlamp")

    def test_declared_gives_check_ids_and_empty_connect_form(self) -> None:
        declared = drafts.Declared(_review()["meta"])
        self.assertEqual(set(declared.checks({}, {})), {"glow"})
        self.assertEqual(declared.CONNECT["fields"], [])

    def test_name_of_existing_family(self) -> None:
        bad = FAMILY_PY.replace('NAME = "testlamp"', 'NAME = "pins"')
        joined = "\n".join(_review(family_py=bad)["problems"])
        self.assertIn("already a family", joined)

    def test_check_wrong_params(self) -> None:
        bad = FAMILY_PY.replace(
            "def check(device, config, check_id):",
            "def check(device, wrong):")
        problems = _review(family_py=bad)["problems"]
        joined = "\n".join(problems)
        # check must take (device, config, check_id)
        self.assertIn("check", joined)
        self.assertIn("must start with", joined)

    def test_driver_without_sdk(self) -> None:
        joined = "\n".join(_review(driver_py="x = 1\n")["problems"])
        self.assertIn("body SDK", joined)

    def test_computed_constant(self) -> None:
        bad = FAMILY_PY.replace(
            'GUIDE = "A pretend USB lamp for tests: plug it in, connect, '
            'then run the glow check and look at the light."',
            'GUIDE = "lamp " * 12')
        joined = "\n".join(_review(family_py=bad)["problems"])
        self.assertIn("plain literals", joined)

    def test_review_lists_subprocess_in_reaches(self) -> None:
        reaches = _review()["reaches"]
        self.assertTrue(any("subprocess" in r for r in reaches), reaches)


@unittest.skipUnless(shutil.which("bwrap") or shutil.which("bubblewrap"),
                     "bubblewrap isn't installed")
class SandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        # Import here so the module import order matches the workshop.
        sys.path.insert(0, str(ROOT / "bodies" / "workshop"))
        from workshop import drafting  # noqa: E402

        self.drafting = drafting
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.drafts_dir = base / "drafts"
        self.families_dir = base / "families"
        self.drafts_dir.mkdir()
        self.families_dir.mkdir()
        self._orig_drafts_root = drafting.drafts_root
        self._orig_local_root = drafting.local_root
        drafting.drafts_root = lambda: self.drafts_dir  # type: ignore
        drafting.local_root = lambda: self.families_dir  # type: ignore
        self.addCleanup(self._restore_roots)
        self.draft_id = "testbuild1"
        folder = drafting.draft_folder(self.draft_id)
        folder.mkdir(parents=True)
        (folder / "family.py").write_text(FAMILY_PY, encoding="utf-8")
        (folder / "driver.py").write_text(DRIVER_PY, encoding="utf-8")

    def _restore_roots(self) -> None:
        self.drafting.drafts_root = self._orig_drafts_root  # type: ignore
        self.drafting.local_root = self._orig_local_root  # type: ignore

    def _approve(self) -> None:
        folder = self.drafting.draft_folder(self.draft_id)
        reviewed = self.drafting.review_folder(folder)
        self.assertEqual(reviewed["problems"], [])
        (folder / "approved.json").write_text(
            json.dumps({"hash": reviewed["hash"], "at": time.time()}) + "\n",
            encoding="utf-8")

    def test_connect_before_approval_raises(self) -> None:
        with self.assertRaises(Exception):
            fam = self.drafting.resolve(f"draft:{self.draft_id}")
            fam.connect(dict(DEVICE), {})

    def test_approve_connect_hides_ssh_and_check_image_bytes(self) -> None:
        self._approve()
        fam = self.drafting.resolve(f"draft:{self.draft_id}")
        _text, device = fam.connect(dict(DEVICE), {})
        self.assertTrue(device["ready"])
        # The sandboxed code looked for ~/.ssh and could not see it.
        self.assertIs(device.get("ssh_seen"), False)
        result = fam.check(device, {}, "glow")
        self.assertIsInstance(result["image"], bytes)
        self.assertEqual(result["image"], b"\xff\xd8\xff\xe0testlamp")
        self.assertIsInstance(result["expect"], str)

    def test_editing_a_file_voids_approval(self) -> None:
        self._approve()
        fam = self.drafting.resolve(f"draft:{self.draft_id}")
        self.assertTrue(fam.check(dict(DEVICE), {}, "glow")["expect"])
        folder = self.drafting.draft_folder(self.draft_id)
        with (folder / "family.py").open("a", encoding="utf-8") as fh:
            fh.write("\n# edited after approval\n")
        with self.assertRaises(Exception):
            fam2 = self.drafting.resolve(f"draft:{self.draft_id}")
            fam2.check(dict(DEVICE), {}, "glow")

    def test_promotion(self) -> None:
        self._approve()
        ref = self.drafting.promote(self.draft_id, "testbuild")
        self.assertEqual(ref, "local:testlamp")
        self.assertIn("testlamp", self.drafting.local_ids())
        provenance = self.drafting.local_folder("testlamp") / "provenance.json"
        self.assertTrue(provenance.is_file())
        data = json.loads(provenance.read_text(encoding="utf-8"))
        self.assertEqual(data["draft"], self.draft_id)


if __name__ == "__main__":
    unittest.main()


class LinkHopsTests(unittest.TestCase):
    """The sandbox finds a venv's real python behind any symlinks (uv, pyenv, Homebrew)."""

    def test_every_link_on_the_way_in_order(self):
        from workshop import sandbox

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "real-3.11.15" / "bin").mkdir(parents=True)
            (root / "real-3.11.15" / "bin" / "python3.11").write_text("")
            (root / "real-3.11").symlink_to("real-3.11.15")                      # a folder alias
            (root / "bin").mkdir()
            (root / "bin" / "python3.11").symlink_to(root / "real-3.11" / "bin" / "python3.11")
            (root / "venv" / "bin").mkdir(parents=True)
            (root / "venv" / "bin" / "python").symlink_to(root / "bin" / "python3.11")
            hops = sandbox.link_hops(root / "venv" / "bin" / "python")
        self.assertEqual(hops, [
            (str(root / "venv" / "bin" / "python"), str(root / "bin" / "python3.11")),
            (str(root / "bin" / "python3.11"), str(root / "real-3.11" / "bin" / "python3.11")),
            (str(root / "real-3.11"), "real-3.11.15"),
        ])

    def test_a_loop_stops(self):
        from workshop import sandbox

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "a").symlink_to(root / "b")
            (root / "b").symlink_to(root / "a")
            self.assertEqual(len(sandbox.link_hops(root / "a")), 2)
