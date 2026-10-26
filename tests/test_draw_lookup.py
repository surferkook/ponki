"""Drawing and looking up (spec 03 §4b). Paint and search stubbed, no network."""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

import hub.lookup as lookup_mod
import hub.portrait as portrait_mod
from hub.house import House, Link, WATCH_SECONDS
from hub.providers import Step
from test_say import ScriptedProvider, call, make_house, no_speak


def jpeg_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (220, 120, 40)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class PaintCall(unittest.TestCase):
    def test_paint_sends_output_format_natively(self) -> None:
        import base64
        from types import SimpleNamespace

        captured: dict = {}

        class FakeImages:
            async def generate(self, **kwargs):
                captured.update(kwargs)
                buf = io.BytesIO()
                from PIL import Image
                Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, format="PNG")
                return SimpleNamespace(data=[SimpleNamespace(
                    b64_json=base64.b64encode(buf.getvalue()).decode())])

        client = SimpleNamespace(images=FakeImages())
        out = asyncio.run(portrait_mod.paint("buddy", "a small robot", client=client))
        self.assertEqual(captured["output_format"], "png")
        self.assertNotIn("extra_body", captured)
        self.assertTrue(out)


class DrawAndLookupTools(unittest.TestCase):
    def test_every_robot_has_draw_and_look_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            buddy = house.soul("buddy")  # no camera, only a face action
            names = [t.name for t in buddy.build_tools(house.manifest("buddy"))]
            self.assertIn("draw", names)
            self.assertIn("look_up", names)
            self.assertNotIn("locate", names)  # no camera

    def test_a_drawing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            no_speak(house)
            painted: dict = {}

            async def fake_paint(name, picture, side=512, fmt="WEBP", label="portrait"):
                painted.update(name=name, picture=picture, side=side, fmt=fmt, label=label)
                return jpeg_bytes()
            self.addCleanup(setattr, portrait_mod, "paint", portrait_mod.paint)
            portrait_mod.paint = fake_paint
            provider.scripts["buddy"] = [
                call("draw", picture="a crayon dragon", **{"for": "Darko"}),
                Step(text="There's your dragon!"),
            ]

            asyncio.run(house.soul("buddy").turn("draw me a dragon", "human"))
            self.assertEqual(painted["fmt"], "JPEG")
            self.assertEqual(painted["picture"], "a crayon dragon")
            drew = [e for e in house.events if e.get("kind") == "drew"]
            self.assertEqual(len(drew), 1)
            self.assertEqual(drew[0]["to"], "Darko")
            url = drew[0]["image"]
            self.assertTrue(url.startswith("/api/robots/buddy/seen/"))
            n = url.rsplit("/", 1)[1].split(".")[0]
            self.assertTrue((Path(tmp) / "seen" / "buddy" / f"{n}.jpg").exists())
            conv = house.soul("buddy")._conv
            self.assertIsNotNone(conv.tool_results[-1].image_b64)  # it sees its drawing
            self.assertAlmostEqual(house.soul("buddy").data["usage"]["usd"], 0.01)

    def test_an_answer_with_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            no_speak(house)
            asked: dict = {}

            async def fake_ask(question, who=""):
                asked["question"] = question
                return {"answer": "Rain tomorrow afternoon.",
                        "sources": [{"title": "KNMI forecast",
                                     "url": "https://example.com/knmi"}],
                        "searches": 1, "usd": 0.003, "ms": 42}
            self.addCleanup(setattr, lookup_mod, "ask", lookup_mod.ask)
            lookup_mod.ask = fake_ask
            provider.scripts["buddy"] = [
                call("look_up", question="weather Chicago tomorrow"),
                Step(text="Take an umbrella."),
            ]

            asyncio.run(house.soul("buddy").turn("will it rain?", "human"))
            self.assertEqual(asked["question"], "weather Chicago tomorrow")
            conv = house.soul("buddy")._conv
            self.assertIn("Rain tomorrow afternoon.", conv.tool_results[-1].text)
            self.assertIn("KNMI forecast", conv.tool_results[-1].text)
            looked = [e for e in house.events if e.get("kind") == "looked_up"]
            self.assertEqual(looked[0]["sources"][0]["url"], "https://example.com/knmi")

    def test_a_failed_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())

            async def broken(question, who=""):
                raise RuntimeError("search is down")
            self.addCleanup(setattr, lookup_mod, "ask", lookup_mod.ask)
            lookup_mod.ask = broken

            result = asyncio.run(house.look_up("buddy", "weather tomorrow"))
            self.assertFalse(result["ok"])
            self.assertIn("search is down", result["text"])


class MovedFromOutside(unittest.TestCase):
    def _house_with_state(self, tmp):
        provider = ScriptedProvider()
        house = make_house(tmp, provider)
        no_speak(house)
        man = dict(house.manifest("buddy"))
        house.manifests["buddy"] = man
        link = Link(ws=object(), manifest=man, state={"pose": "cradle"})
        house.bodies["buddy"] = link
        return provider, house, link

    def test_outside_change_is_noted_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider, house, link = self._house_with_state(tmp)
            buddy = house.soul("buddy")

            async def act(key, action, args=None, timeout=None):
                return {"ok": True, "text": "done", "data": "done"}
            house.act = act

            def moved():  # the body reports a new pose mid-turn, before the action
                link.state["pose"] = "stand"
            provider.step_hooks["buddy"] = moved
            provider.scripts["buddy"] = [call("face", expression="happy"),
                                          Step(text="Hello.")]

            async def run():
                await buddy.turn("hi", "human")
                provider.scripts["buddy"] = [Step(text="Again.")]
                await buddy.turn("hi again", "human")
            asyncio.run(run())
            notes = "\n".join(buddy._conv.notes)
            self.assertIn('"stand"', notes)

    def test_own_action_change_is_not_retold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider, house, link = self._house_with_state(tmp)
            buddy = house.soul("buddy")

            async def act(key, action, args=None, timeout=None):
                link.state["pose"] = "stand"  # its own action moved it
                return {"ok": True, "text": "done", "data": "done"}
            house.act = act
            provider.scripts["buddy"] = [call("face", expression="happy"),
                                          Step(text="Hello.")]

            async def run():
                await buddy.turn("hi", "human")
                provider.scripts["buddy"] = [Step(text="Again.")]
                await buddy.turn("hi again", "human")
            asyncio.run(run())
            notes = "\n".join(buddy._conv.notes)
            self.assertNotIn('"stand"', notes)


class WatchWaits(unittest.TestCase):
    def test_watch_waits_for_its_own_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            starts: dict = {}

            class RecordWS:
                async def close(self, code=1000):
                    pass

                async def send_json(self, payload):
                    if payload.get("type") == "act":
                        starts[payload["action"]] = (time.time(), payload["args"])

                        async def reply():
                            await house.on_body_message(
                                "kijkie", {"type": "result", "id": payload["id"],
                                            "ok": True, "data": "clip"})
                        asyncio.ensure_future(reply())

            man = {"protocol": 1, "name": "kijkie", "display_name": "Kijkie",
                   "description": "A camera.",
                   "actions": {"record": {"description": "Record.", "hidden": True,
                                          "params": {"type": "object", "properties": {}}}},
                   "state": {}}
            house.manifests["kijkie"] = man

            async def run():
                house.bodies["kijkie"] = Link(ws=RecordWS(), manifest=man, state={})
                house._speaking["kijkie"] = 1

                async def stop_speaking():
                    await asyncio.sleep(1.0)
                    house._speaking["kijkie"] = 0
                asyncio.ensure_future(stop_speaking())
                began = time.time()
                result = await house.watch("kijkie", "a kick")
                return began, result

            began, result = asyncio.run(run())
            self.assertTrue(result["ok"])
            started_at, args = starts["record"]
            self.assertGreaterEqual(started_at - began, 1.0)
            self.assertEqual(args["seconds"], WATCH_SECONDS)  # 12 by default


if __name__ == "__main__":
    unittest.main()
