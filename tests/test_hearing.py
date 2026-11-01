"""Hearing: name matching, routing, push-to-talk guards, the ears (spec 06).

No services, no network: the ears and the HTTP call are stubbed.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "ears"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "mic"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

import ears_server as ears  # noqa: E402
from hub import hearing as hearing_mod  # noqa: E402
from hub import house as house_mod  # noqa: E402
from hub.hearing import HearingError, addressed  # noqa: E402
from mic_fixture import SentenceCutter  # noqa: E402
from test_say import ScriptedProvider, make_house  # noqa: E402

ROBOTS = {"keiki": "Keiki", "vroom-vroom": "Vroom-Vroom",
          "robohon": "RoBoHoN", "buddy": "Buddy"}


class Addressed(unittest.TestCase):
    def test_names_as_people_say_them(self) -> None:
        self.assertEqual(addressed("Keiki, where's the orange?", ROBOTS), "keiki")
        self.assertEqual(addressed("kaiki look left", ROBOTS), "keiki")
        self.assertEqual(addressed("room-room drive forward", ROBOTS), "vroom-vroom")
        self.assertEqual(addressed("vroom vroom", ROBOTS), "vroom-vroom")
        self.assertEqual(addressed("robo hon, kick", ROBOTS), "robohon")

    def test_no_name_no_match(self) -> None:
        self.assertIsNone(addressed("the living room is dark", ROBOTS))
        self.assertIsNone(addressed("body", ROBOTS))  # not Buddy

    def test_earliest_wins(self) -> None:
        self.assertEqual(addressed("Buddy tell Keiki", ROBOTS), "buddy")


class HearRouting(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.house = make_house(self._tmp.name, ScriptedProvider())
        self.turns: list = []

        async def fake_turn(rid, text, source):
            self.turns.append((rid, text, source))
            return ""

        self.house._run_turn = fake_turn

    def hear(self, text, **kwargs):
        async def run():
            target = self.house.hear(text, **kwargs)
            await asyncio.sleep(0.05)  # let the spawned turn run
            return target

        return asyncio.run(run())

    def test_named_wins_over_page(self) -> None:
        target = self.hear("Buddy, say hi", to="eye", via="push-to-talk")
        self.assertEqual(target, "buddy")
        self.assertEqual(self.turns[0][0], "buddy")
        heard = [e for e in self.house.events if e["kind"] == "heard"]
        self.assertEqual(heard[0]["via"], "push-to-talk")

    def test_page_when_nobody_named(self) -> None:
        self.assertEqual(self.hear("what do you see", to="eye"), "eye")

    def test_last_addressed_window(self) -> None:
        self.house._last_addressed = ("buddy", time.time() - 60)
        self.assertEqual(self.hear("and again please"), "buddy")
        self.house._last_addressed = ("buddy", time.time() - 601)
        self.assertEqual(self.hear("and again please"), "everyone")
        heard = [e for e in self.house.events if e["kind"] == "heard"]
        self.assertEqual(heard[-1]["to"], "everyone")

    def test_room_words_beat_the_last_one_spoken_to(self) -> None:
        self.house._last_addressed = ("buddy", time.time() - 60)
        self.assertEqual(self.hear("it's not bedtime yet, everybody back up"), "everyone")
        self.assertEqual(self.turns, [])
        self.assertIsNone(self.house._last_addressed)

    def test_everyone_hears_it_and_the_floor_picks_who_answers(self) -> None:
        """Seen live: "oh you guys it's not bedtime yet … everybody back up" reached no one."""
        for rid in ("eye", "buddy"):
            self.house.bodies[rid] = house_mod.Link(ws=object(), manifest=self.house.manifests[rid], state={})
        asked = []
        self.house.provider.floor_handler = lambda request: (
            asked.append(request) or '{"react": "buddy", "also": [], "why": "to the house"}')
        answered = []

        async def floor_turn(rid, speaker, text, depth, promised):
            answered.append((rid, speaker))
        self.house._floor_turn = floor_turn
        self.hear("oh you guys it's not bedtime yet, everybody back up")
        self.assertEqual(asked[0]["said_to"], "everyone")
        self.assertEqual(answered, [("buddy", self.house.human)])
        for rid in ("eye", "buddy"):
            self.assertEqual(self.house.soul(rid)._heard[-1]["who"], self.house.human)

    def test_a_stop_for_everyone_stops_everything(self) -> None:
        stopped = []

        async def stop_everything():
            stopped.append(True)
        self.house.stop_everything = stop_everything
        self.hear("guys, stop!")
        self.assertEqual(stopped, [True])


class HeardAudio(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.house = make_house(self._tmp.name, ScriptedProvider())
        self.house.settings["always_on"] = True
        self.house.fixtures["brio"] = SimpleNamespace(
            manifest={"kind": "mic"}, state={}, pending={})
        # fixtures_of reads .manifest via Link objects in house; use real Link
        from hub.house import Link
        self.house.fixtures["brio"] = Link(
            ws=object(), manifest={"kind": "mic", "name": "brio"}, state={})
        self.turns: list = []

        async def fake_turn(rid, text, source):
            self.turns.append((rid, text))
            return ""

        self.house._run_turn = fake_turn
        self._transcribe = hearing_mod.transcribe
        self.addCleanup(setattr, hearing_mod, "transcribe", self._transcribe)

    def stub_ears(self, text="Buddy wave", fail=False):
        async def fake(audio, keywords=None):
            if fail:
                raise HearingError("the house's ears aren't reachable (ConnectError)")
            return {"text": text, "language": "en", "seconds": 1.0, "ms": 5,
                    "engine": "muse", "billed_seconds": 1, "session_id": "s"}

        hearing_mod.transcribe = fake

    def test_ignored_while_voice_lock_held(self) -> None:
        self.stub_ears()

        async def run():
            await self.house._voice_lock.acquire()
            try:
                return await self.house.heard_audio("brio", "AAAA", 1.0)
            finally:
                self.house._voice_lock.release()

        self.assertTrue(asyncio.run(run()).get("ignored"))

    def test_ignored_inside_a_voice_span(self) -> None:
        self.stub_ears()
        now = time.time()
        self.house._voice_spans.append((now - 10, now - 1, "buddy"))
        result = asyncio.run(self.house.heard_audio("brio", "AAAA", 5.0))
        self.assertTrue(result.get("ignored"))
        self.house._voice_spans.clear()
        self.house._voice_spans.append((now - 100, now - 90, "buddy"))
        result = asyncio.run(self.house.heard_audio("brio", "AAAA", 5.0))
        self.assertFalse(result.get("ignored", False))

    def test_one_word_drop_and_keep(self) -> None:
        self.stub_ears(text="you")
        result = asyncio.run(self.house.heard_audio("brio", "AAAA", 1.0))
        self.assertTrue(result.get("dropped"))
        result = asyncio.run(self.house.heard_audio(
            "brio", "AAAA", 1.0, push=True))
        self.assertEqual(result["text"], "you")  # push-to-talk is always acted on
        self.stub_ears(text="Buddy")
        result = asyncio.run(self.house.heard_audio("brio", "AAAA", 1.0))
        self.assertEqual(result.get("to"), "buddy")  # a name alone is kept

    def test_ears_failure_emits_error(self) -> None:
        self.stub_ears(fail=True)
        result = asyncio.run(self.house.heard_audio("brio", "AAAA", 1.0))
        self.assertIn("error", result)
        errors = [e for e in self.house.events if e["kind"] == "error"]
        self.assertTrue(errors)


def _wav(rate=16000, channels=1, seconds=0.1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x20" * int(rate * seconds) * channels)
    return buf.getvalue()


class EarsService(unittest.TestCase):
    def setUp(self) -> None:
        self.env = os.environ.get("META_API_KEY")
        os.environ["META_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("META_API_KEY", None)
                        if self.env is None else
                        os.environ.__setitem__("META_API_KEY", self.env))
        self._post = ears._post_muse
        self.addCleanup(setattr, ears, "_post_muse", self._post)

    def test_request_shape(self) -> None:
        req = ears.muse_request(["Keiki", "Buddy"])
        self.assertEqual(req, {
            "model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV",
            "mode": "PUSH_TO_TALK", "languageBias": ["English"],
            "keywords": ["Keiki", "Buddy"]})

    def test_wav_passthrough_and_conversion(self) -> None:
        mono16 = _wav(16000, 1)
        self.assertEqual(ears.prepare_for_muse(mono16), mono16)
        mono24 = _wav(24000, 1)
        self.assertEqual(ears.prepare_for_muse(mono24), mono24)
        out = ears.prepare_for_muse(_wav(48000, 2))
        with wave.open(io.BytesIO(out), "rb") as reader:
            self.assertEqual(reader.getframerate(), 24000)
            self.assertEqual(reader.getnchannels(), 1)

    def test_billed_seconds_and_transcript(self) -> None:
        async def fake_post(wav_bytes, keywords, session_id):
            return SimpleNamespace(status_code=200, json=lambda: {
                "transcript": "Keiki look", "audioDurationMs": 5500,
                "sessionId": "house-1"}, text="")

        result = asyncio.run(ears.muse_transcribe(_wav(), ["Keiki"],
                                                  post=fake_post))
        self.assertEqual(result["text"], "Keiki look")
        self.assertEqual(result["billed_seconds"], 5)

    def test_429_retries_once(self) -> None:
        calls = []

        async def fake_post(wav_bytes, keywords, session_id):
            calls.append(1)
            return SimpleNamespace(status_code=429, json=lambda: {},
                                   text="rate limited")

        with self.assertRaises(ears.MuseError) as ctx:
            asyncio.run(ears.muse_transcribe(_wav(), [], post=fake_post))
        self.assertEqual(len(calls), 2)  # one retry
        self.assertIn("429", str(ctx.exception))

    def test_no_local_fallback(self) -> None:
        async def fake_post(wav_bytes, keywords, session_id):
            return SimpleNamespace(status_code=429, json=lambda: {},
                                   text="rate limited")

        saved = ears._post_muse
        self.addCleanup(setattr, ears, "_post_muse", saved)
        ears._post_muse = fake_post
        import base64
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(ears.transcribe({
                "audio": base64.b64encode(_wav()).decode(), "keywords": []}))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("429", ctx.exception.detail)
        self.assertFalse(hasattr(ears, "whisper_transcribe"))


class SentenceCutting(unittest.TestCase):
    def test_sentence_with_history_and_quiet_end(self) -> None:
        cutter = SentenceCutter()
        cutter.set_listening(True)
        event = None
        for _ in range(10):  # quiet history
            event = cutter.feed(10)
        first_speech = cutter.idx
        for _ in range(30):  # 0.9 s of speech
            event = cutter.feed(300) or event
        for _ in range(40):  # quiet ends it after 0.8 s
            event = cutter.feed(10) or event
        self.assertIsNotNone(event)
        self.assertLess(event["start"], first_speech)  # history included
        self.assertGreaterEqual(event["start"], first_speech - 10)
        self.assertGreater(event["end"], first_speech)

    def test_short_blip_not_sent(self) -> None:
        cutter = SentenceCutter()
        cutter.set_listening(True)
        event = None
        for _ in range(5):
            cutter.feed(10)
        for _ in range(8):  # just enough to start, too short to send
            cutter.feed(300)
        for _ in range(40):
            event = cutter.feed(10) or event
        self.assertIsNone(event)

    def test_thirty_second_cut(self) -> None:
        cutter = SentenceCutter()
        cutter.set_listening(True)
        event = None
        frames = 0
        while event is None and frames < 1200:
            event = cutter.feed(300)
            frames += 1
        self.assertIsNotNone(event)
        self.assertLessEqual(frames, 1010)

    def test_floor_adapts(self) -> None:
        cutter = SentenceCutter()
        for _ in range(200):
            cutter.feed(30)
        self.assertGreater(cutter.floor, 25)  # followed the room up from 20


if __name__ == "__main__":
    unittest.main()
