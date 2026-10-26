"""Soul, room and floor tests (spec 03). Scripted models, no network."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import hub.soul as soul_mod
from hub import pricing
from hub.house import House, Link
from hub.providers import Step, ToolCall, ToolResult
from hub.providers.meta import MetaProvider


# -- a scripted brain ---------------------------------------------------------

class ScriptedConversation:
    def __init__(self, provider, system, tools) -> None:
        self.provider = provider
        self.label = ""
        self.system = system
        self.tools = tools
        self.records: list = []
        self.users: list[str] = []
        self.notes: list[str] = []
        self.tool_results: list = []

    def add_user(self, text: str) -> None:
        self.users.append(text)
        self.records.append({"role": "user", "content": text})

    def add_note(self, text: str) -> None:
        self.notes.append(text)
        self.records.append({"role": "system", "content": text})

    def add_tool_results(self, results) -> None:
        self.tool_results.extend(results)
        for r in results:
            self.records.append({"role": "tool", "content": r.text})

    async def step(self) -> Step:
        hook = self.provider.step_hooks.get(self.label)
        if hook:
            hook()
            self.provider.step_hooks.pop(self.label, None)
        queue = self.provider.scripts.get(self.label) or []
        if queue:
            return queue.pop(0)
        return Step(text="(silence)")

    def checkpoint(self) -> int:
        return len(self.records)

    def rollback(self, mark: int) -> None:
        del self.records[mark:]

    def has_history(self) -> bool:
        return False

    def dump(self) -> dict:
        return {"provider": "scripted", "model": "scripted-1",
                "messages": list(self.records)}

    def forget_image(self, fp: str, note: str) -> int:
        return 0


class ScriptedProvider:
    name = "scripted"
    model = "scripted-1"

    def __init__(self) -> None:
        self.scripts: dict[str, list[Step]] = {}
        self.step_hooks: dict = {}
        self.conversations: dict[str, ScriptedConversation] = {}
        self.floor_calls: list[dict] = []
        self.floor_handler = None

    def conversation(self, system, tools, saved=None):
        conv = ScriptedConversation(self, system, tools)
        self._pending = conv
        return conv

    async def complete(self, system, prompt, max_tokens=4000, who=""):
        if who == "floor":
            request = json.loads(prompt)
            self.floor_calls.append(request)
            if self.floor_handler:
                return self.floor_handler(request)
            return '{"react": null, "also": [], "why": "quiet"}'
        return "You are a test robot. Scripted and cheerful."


def call(name: str, **args) -> Step:
    return Step(tool_calls=[ToolCall(id=f"c{name}", name=name, args=args)])


MANIFESTS = {
    "eye": {"protocol": 1, "name": "eye", "display_name": "Eye",
            "description": "Eye is a camera.", "actions": {}, "state": {}},
    "buddy": {"protocol": 1, "name": "buddy", "display_name": "Buddy",
              "description": "A desk buddy with a face.",
              "actions": {"face": {"description": "Set the face.",
                                   "params": {"type": "object",
                                              "properties": {"expression": {"type": "string"}},
                                              "required": ["expression"],
                                              "additionalProperties": False}}},
              "state": {}},
}


def make_house(tmp: str, provider) -> House:
    root = Path(tmp)
    (root / "souls").mkdir(parents=True, exist_ok=True)
    for rid, display in (("eye", "Eye"), ("buddy", "Buddy")):
        (root / "souls" / f"{rid}.json").write_text(json.dumps({
            "persona": f"You are {display}.", "born": 1.0,
            "journal": [{"t": 1.0, "from": "human", "in": "hi", "out": "hello"}],
            "conversation": None, "news": []}), encoding="utf-8")
    house = House(provider, "Darko", root)
    for rid, manifest in MANIFESTS.items():
        house.manifests[rid] = manifest
        house.registry.adopt(rid, kind="robot", display_name=manifest["display_name"])
    return house


def no_speak(house, order=None):
    async def speak(rid, text):
        if order is not None:
            order.append(("speak", text))
    house.speak = speak


class SayActSay(unittest.TestCase):
    def test_say_act_say_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            order: list = []
            no_speak(house, order)

            async def act(key, action, args=None, timeout=None):
                order.append(("act", action, args))
                return {"ok": True, "text": "done", "data": "done"}
            house.act = act
            provider.scripts["buddy"] = [
                call("say", text="This is happy!"),
                call("face", expression="happy"),
                call("say", text="And this is wink."),
                call("face", expression="wink"),
                Step(text="(silence)"),
            ]

            async def run():
                conv_owner = house.soul("buddy")
                result = await conv_owner.turn("show me your faces", "human")
                return result

            result = asyncio.run(run())
            self.assertEqual(result, "")
            self.assertEqual(order, [("speak", "This is happy!"),
                                     ("act", "face", {"expression": "happy"}),
                                     ("speak", "And this is wink."),
                                     ("act", "face", {"expression": "wink"})])
            journal = house.soul("buddy").data["journal"]
            self.assertEqual(journal[-1]["out"], "This is happy! / And this is wink.")
            mids = [e for e in house.events if e.get("kind") == "say"]
            self.assertEqual(len(mids), 2)
            self.assertTrue(all(e.get("mid") for e in mids))


class FloorGating(unittest.TestCase):
    def test_named_exchanges_go_on_chatter_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            for rid in ("eye", "buddy"):
                house.bodies[rid] = Link(ws=object(), manifest=MANIFESTS[rid], state={})
            calls: list = []

            async def floor(speaker, text, listeners, open_, depth, to):
                calls.append((depth, open_))
            house._run_floor = floor

            async def run():
                house.said("buddy", "Is that the orange, Eye?", "human", 5, False)
                house.said("buddy", "What a lovely day.", "human", 5, False)
                house.said("buddy", "Eye, again?", "human", 10, False)
                house.said("buddy", "Sunny today.", "human", 1, False)
                house.soul("eye").promises.append(
                    {"text": "when a task finishes, celebrate", "habit": False})
                house.said("buddy", "a private word", "eye", 0, True)
                await asyncio.sleep(0.1)

            asyncio.run(run())
            self.assertEqual(sorted(calls), [(0, False), (1, True), (5, True)])
            self.assertFalse(house._names_housemate("I'm eyeish", ["eye"]))

    def test_promises_kept_via_floor_also(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            for rid in ("eye", "buddy"):
                house.bodies[rid] = Link(ws=object(), manifest=MANIFESTS[rid], state={})
            no_speak(house)
            provider.scripts["buddy"] = [
                call("promise", keep="when a housemate finishes a task, celebrate"),
                Step(text="Done."),
                Step(text="Hooray!"),
            ]

            async def run():
                await house.soul("buddy").turn("keep that promise", "human")
                buddy_conv = house.soul("buddy")._conv
                self.assertIn("1. when a housemate finishes a task, celebrate",
                              buddy_conv.tool_results[-1].text)

                def floor_handler(request):
                    self.assertIn("celebrate", json.dumps(request))
                    return '{"react": null, "also": ["buddy", "eye"], "why": "promise"}'
                provider.floor_handler = floor_handler
                await house._run_floor("eye", "Vroom-Vroom made it!", ["buddy"], False, 0, "human")
                await asyncio.sleep(0.1)
                # drop it again through the tool
                provider.scripts["buddy"].extend(
                    [call("promise", drop=1), Step(text="Dropped.")])
                await house.soul("buddy").turn("let it go", "human")

            asyncio.run(run())
            journal = house.soul("buddy").data["journal"]
            self.assertTrue(any(e.get("from") == "heard:eye" for e in journal))
            eye_journal = house.soul("eye").data["journal"]
            self.assertEqual(len(eye_journal), 1)  # eye never took a turn
            self.assertEqual(house.soul("buddy").promises, [])


class TakingTurns(unittest.TestCase):
    def test_speaking_blocks_housemates_not_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            order: list = []

            async def run():
                house._speaking["eye"] = 1

                async def clear():
                    await asyncio.sleep(0.3)
                    house._speaking["eye"] = 0
                asyncio.ensure_future(clear())

                async def mover(rid, label):
                    await asyncio.sleep(0.05)
                    await house._wait_turn(rid)
                    order.append(label)
                await asyncio.gather(mover("eye", "eye-move"), mover("buddy", "buddy-move"))

            started = time.time()
            asyncio.run(run())
            self.assertEqual(order[0], "eye-move")   # its own line never blocks it
            self.assertEqual(order[-1], "buddy-move")
            self.assertGreater(time.time() - started, 0.6)  # speech + the breath

    def test_sounds_hold_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            done: list = []

            class TuneWS:
                async def close(self, code=1000):
                    pass

                async def send_json(self, payload):
                    if payload.get("type") == "act":
                        async def reply():
                            await asyncio.sleep(0.3)
                            done.append("tune")
                            await house.on_body_message(
                                "buddy", {"type": "result", "id": payload["id"],
                                          "ok": True, "data": "tune done"})
                        asyncio.ensure_future(reply())

            class InstantWS:
                async def close(self, code=1000):
                    pass

                async def send_json(self, payload):
                    if payload.get("type") == "act":
                        async def reply():
                            await house.on_body_message(
                                "eye", {"type": "result", "id": payload["id"],
                                        "ok": True, "data": "waved"})
                        asyncio.ensure_future(reply())

            buddy_man = dict(MANIFESTS["buddy"])
            buddy_man["actions"] = {"tune": {"description": "Play a tune.", "sound": True,
                                             "params": {"type": "object", "properties": {}}}}
            eye_man = dict(MANIFESTS["eye"])
            eye_man["actions"] = {"wave": {"description": "Wave.",
                                           "params": {"type": "object", "properties": {}}}}
            house.manifests["buddy"], house.manifests["eye"] = buddy_man, eye_man

            async def run():
                house.bodies["buddy"] = Link(ws=TuneWS(), manifest=buddy_man, state={})
                house.bodies["eye"] = Link(ws=InstantWS(), manifest=eye_man, state={})
                tune = asyncio.ensure_future(house.act("buddy", "tune", {}))
                await asyncio.sleep(0.05)

                async def speak_eye():
                    await house.speak("eye", "hello there")
                    done.append("speak")
                speak = asyncio.ensure_future(speak_eye())
                await tune
                await speak

            asyncio.run(run())
            self.assertEqual(done[0], "tune")  # nobody speaks over a tune
            self.assertIn("speak", done)


class HabitsAndPrompt(unittest.TestCase):
    def test_habit_fires_from_an_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)
            no_speak(house)
            buddy = house.soul("buddy")
            buddy.promises.append({"text": "when the room goes dark, say goodnight",
                                   "habit": True})
            provider.scripts["buddy"] = [Step(text="Goodnight, everyone.")]

            asyncio.run(buddy.turn("the room went dark", "event"))
            conv = buddy._conv
            self.assertIn("1. [habit] when the room goes dark, say goodnight",
                          conv.users[-1])

    def test_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            buddy = house.soul("buddy")
            chirping = dict(MANIFESTS["buddy"], voice="chirp", voice_style="chirps")
            system = buddy.build_system(chirping)
            self.assertIn("chirps along", system)
            self.assertIn("explain it simply and honestly", system)
            self.assertIn("let the action speak for itself", system)
            plain = buddy.build_system(MANIFESTS["buddy"])
            self.assertIn("your body has no speaker", plain)


class ProviderShapes(unittest.TestCase):
    def test_meta_conversation_shapes(self) -> None:
        captured: dict = {}

        class FakeCompletions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="hi", tool_calls=None))],
                    usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=10,
                                          prompt_tokens_details=SimpleNamespace(
                                              cached_tokens=800)))

        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        provider = MetaProvider("muse-spark-1.3", "low", client=client)
        conv = provider.conversation("sys", [], label="eye")
        conv.add_tool_results([ToolResult(id="1", text="looked", image_b64="QUJD")])
        self.assertTrue(conv.messages[-2]["content"].endswith("(photo attached below)"))
        self.assertEqual(conv.messages[-1]["content"][1]["type"], "image_url")
        self.assertIn("data:image/jpeg;base64,", conv.messages[-1]["content"][1]
                      ["image_url"]["url"])
        conv2 = provider.conversation("sys", [], label="eye")
        conv2.add_tool_results([ToolResult(id="2", text="failed", image_b64="QUJD",
                                           is_error=True)])
        self.assertNotIn("image_url", json.dumps(conv2.messages))
        saved = {"provider": "meta", "model": "someone-else", "messages": [{"role": "x"}]}
        conv3 = provider.conversation("sys", [], saved=saved, label="eye")
        self.assertFalse(conv3.has_history())

        async def run():
            return await conv.step()
        step = asyncio.run(run())
        self.assertEqual(step.usage["in"], 200)
        self.assertEqual(step.usage["cache_read"], 800)
        self.assertEqual(captured["prompt_cache_key"], "house-eye")
        self.assertNotIn("extra_body", captured)

    def test_contributor_and_bad_effort_refused(self) -> None:
        with self.assertRaises(ValueError):
            MetaProvider("muse-spark-1.3-contributor", "low", client=object())
        with self.assertRaises(ValueError):
            MetaProvider("muse-spark-1.3", "none", client=object())

    def test_pricing(self) -> None:
        self.assertAlmostEqual(
            pricing.cost("muse-spark-1.3",
                         {"in": 1_000_000, "out": 1_000_000, "cache_read": 1_000_000}),
            1.25 + 4.25 + 0.15)


class AskRouting(unittest.TestCase):
    def test_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            no_speak(house)

            async def run():
                with self.assertRaises(LookupError) as ctx:
                    await house.ask("buddy", "buddy", "hi", ())
                self.assertIn("that's you", str(ctx.exception))
                with self.assertRaises(LookupError) as ctx:
                    await house.ask("buddy", "nobody", "hi", ())
                self.assertIn("no robot called 'nobody'", str(ctx.exception))
                with self.assertRaises(LookupError) as ctx:
                    await house.ask("buddy", "eye", "hi", ("eye",))
                self.assertIn("already waiting on you", str(ctx.exception))
                with self.assertRaises(LookupError) as ctx:
                    await house.ask("buddy", "eye", "hi", ("a", "b", "c"))
                self.assertIn("long chain", str(ctx.exception))

            asyncio.run(run())

    def test_busy_target_fails_fast_when_patched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            old = soul_mod.ASK_WAIT
            soul_mod.ASK_WAIT = 0.2
            try:
                async def run():
                    eye = house.soul("eye")
                    await eye.lock.acquire()
                    try:
                        with self.assertRaises(LookupError) as ctx:
                            await eye.turn("are you there?", source="buddy")
                        self.assertIn("busy right now", str(ctx.exception))
                    finally:
                        eye.lock.release()
                asyncio.run(run())
            finally:
                soul_mod.ASK_WAIT = old


class GuidanceFreshness(unittest.TestCase):
    def test_move_ask_needs_a_fresh_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = make_house(tmp, ScriptedProvider())
            no_speak(house)

            async def run():
                eye = house.soul("eye")
                call = ToolCall("1", "ask_robot",
                                {"robot": "buddy", "message": "turn left 20 degrees"})
                res = await eye._exec_tool(call, "human", ())
                self.assertTrue(res.is_error)
                self.assertIn("fresh look first", res.text)
                eye._view_fresh = True
                res = await eye._exec_tool(call, "human", ())
                self.assertFalse(res.is_error)
                self.assertFalse(eye._view_fresh)  # it moved: photo is old

            asyncio.run(run())


class InterruptTurn(unittest.TestCase):
    def test_person_talking_interrupts_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptedProvider()
            house = make_house(tmp, provider)

            async def slow_speak(rid, text):
                await asyncio.sleep(0.2)
            house.speak = slow_speak

            def endless(rid):
                return [Step(tool_calls=[ToolCall("1", "say", {"text": "working"})])
                        for _ in range(5)]

            async def run():
                buddy = house.soul("buddy")
                provider.scripts["buddy"] = endless("buddy")
                task = asyncio.ensure_future(buddy.turn("keep working", source="human"))
                await asyncio.sleep(0.05)
                buddy.request_interrupt()
                await asyncio.wait_for(task, timeout=5)
                self.assertGreaterEqual(len(provider.scripts["buddy"]), 3)
                self.assertIn("stopped",
                              house.soul("buddy").data["journal"][-1]["out"])

            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
