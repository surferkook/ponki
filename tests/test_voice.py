"""The house voice: voice settings and House.speak (spec 06).

No services: synthesis and body actions are stubbed.
"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "speaker"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bodies" / "sdk"))

from hub import voice as voice_mod  # noqa: E402
from hub.house import Link  # noqa: E402
from hub.voice import VoiceError, clean, default_for  # noqa: E402
from test_say import ScriptedProvider, make_house  # noqa: E402


class CleanVoices(unittest.TestCase):
    def test_unknown_id(self) -> None:
        self.assertIsNone(clean({"id": "nope"}))
        self.assertIsNone(clean(None))
        self.assertIsNone(clean({"id": "warm", "pitch": "loud"}))

    def test_clamping_and_rounding(self) -> None:
        out = clean({"id": "warm", "pitch": 9, "speed": 0.1})
        self.assertEqual(out, {"id": "warm", "pitch": 1.6, "speed": 0.6})
        out = clean({"id": "warm", "pitch": 1.23456, "speed": 1.0})
        self.assertEqual(out["pitch"], 1.23)

    def test_min_pitch_for_births(self) -> None:
        out = clean({"id": "playful", "pitch": 0.8}, min_pitch=1.0)
        self.assertEqual(out["pitch"], 1.0)

    def test_missing_values_default(self) -> None:
        out = clean({"id": "small"})
        self.assertEqual(out, {"id": "small", "pitch": 1.0, "speed": 1.0})

    def test_old_voices_map_to_presets(self) -> None:
        # souls born before the house spoke with MMS keep a voice of their own
        self.assertEqual(clean({"id": "am_puck", "pitch": 1.3}),
                         {"id": "playful", "pitch": 1.3, "speed": 1.0})
        self.assertEqual(clean({"id": "af_sky", "pitch": 1.25})["id"], "warm")
        self.assertEqual(clean({"id": "en_US-ryan-medium"})["id"], "warm")
        self.assertEqual(clean({"id": "puck"})["id"], "playful")


class DefaultVoices(unittest.TestCase):
    def test_stable_known_and_varied(self) -> None:
        self.assertEqual(default_for("keiki"), default_for("keiki"))
        ids = {v["id"] for v in voice_mod.VOICES}
        picks = {default_for(name)["id"]
                 for name in ("buddy", "keiki", "vroom-vroom", "robohon")}
        self.assertTrue(picks <= ids)
        self.assertGreaterEqual(len(picks), 3)


def _wav_ms(samples: int = 4800) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x00\x10" * samples)
    return buf.getvalue()


class SpeakFlow(unittest.TestCase):
    def setUp(self) -> None:
        self._synth = voice_mod.synth
        self.addCleanup(setattr, voice_mod, "synth", self._synth)
        self.acts: list = []

    def _house(self, tmp):
        house = make_house(tmp, ScriptedProvider())

        async def fake_act(key, action, args=None, timeout=20):
            self.acts.append((key, action, args))
            return {"ok": True, "text": "done", "data": None}

        house.act = fake_act
        return house

    def _speaker(self, house, sid="jbl", lead=0.2):
        house.fixtures[sid] = Link(
            ws=object(), manifest={"kind": "speaker", "name": sid},
            state={"lead_in": lead})

    def test_clip_starts_before_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            started = asyncio.Event()

            async def fake_synth(text, v):
                started.set()
                return _wav_ms(), 100

            voice_mod.synth = fake_synth

            async def run():
                await house._voice_lock.acquire()
                task = asyncio.ensure_future(house.speak("buddy", "hello there"))
                await asyncio.wait_for(started.wait(), 2)
                self.assertTrue(started.is_set())  # synth ran, lock still held
                house._voice_lock.release()
                await task

            asyncio.run(run())

    def test_chirps_get_early_and_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            self._speaker(house)
            man = {"name": "buddy", "voice": "chirp", "voice_style": "chirps",
                   "actions": {}}
            house.manifests["buddy"] = man
            house.bodies["buddy"] = Link(ws=object(), manifest=man, state={})

            async def fake_synth(text, v):
                return _wav_ms(), 10

            voice_mod.synth = fake_synth
            asyncio.run(house.speak("buddy", "one two three four five"))
            chirps = [a for a in self.acts if a[1] == "chirp"]
            self.assertEqual(len(chirps), 1)
            self.assertAlmostEqual(chirps[0][2]["seconds"], 0.9)
            self.assertAlmostEqual(chirps[0][2]["early"], 0.2)
            plays = [a for a in self.acts if a[1] == "play"]
            self.assertEqual(plays[0][0], "@jbl")

    def test_floor_held_for_clip_and_lead_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            self._speaker(house)

            async def fake_synth(text, v):
                return _wav_ms(), 100

            voice_mod.synth = fake_synth
            began = time.time()
            asyncio.run(house.speak("buddy", "a short line"))
            self.assertGreaterEqual(time.time() - began, 0.65)  # .1+.2+.4

    def test_clips_keep_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            for _ in range(31):
                house.save_clip(_wav_ms(48))
            folder = Path(tmp) / "voice"
            self.assertFalse((folder / "1.wav").exists())
            self.assertTrue((folder / "31.wav").exists())

    def test_own_voice_offline_makes_no_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            house.manifests["robo"] = {"name": "robo", "voice": "speak",
                                       "actions": {}}
            called = []

            async def fake_synth(text, v):
                called.append(text)
                return _wav_ms(), 10

            voice_mod.synth = fake_synth
            asyncio.run(house.speak("robo", "I have my own speaker"))
            self.assertEqual(called, [])

    def test_voice_play_goes_to_the_body_not_the_speakers(self) -> None:
        # a body with a speaker of its own plays the house voice itself
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            self._speaker(house)
            man = {"name": "robo", "voice_play": "play", "actions": {"play": {}}}
            house.manifests["robo"] = man
            house.bodies["robo"] = Link(ws=object(), manifest=man, state={})

            async def fake_synth(text, v):
                return _wav_ms(), 10

            voice_mod.synth = fake_synth
            asyncio.run(house.speak("robo", "hello from my own speaker"))
            plays = [a for a in self.acts if a[1] == "play"]
            self.assertEqual(len(plays), 1)
            self.assertEqual(plays[0][0], "robo")  # not "@jbl"
            self.assertTrue(plays[0][2]["url"].startswith("/api/voice/"))

    def test_voice_play_offline_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)
            self._speaker(house)
            house.manifests["robo"] = {"name": "robo", "voice_play": "play",
                                       "actions": {"play": {}}}
            called = []

            async def fake_synth(text, v):
                called.append(text)
                return _wav_ms(), 10

            voice_mod.synth = fake_synth
            asyncio.run(house.speak("robo", "nobody hears this"))
            self.assertEqual(called, [])
            self.assertEqual(self.acts, [])

    def test_voice_error_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            house = self._house(tmp)

            async def bad_synth(text, v):
                raise VoiceError("the house voice isn't reachable (ConnectError)")

            voice_mod.synth = bad_synth
            asyncio.run(house.speak("buddy", "hello"))
            self.assertIn("isn't reachable", house.voice_error or "")


class SpeakerLeadIn(unittest.TestCase):
    def test_lead_in_frames_and_params(self) -> None:
        import speaker_fixture

        out = speaker_fixture.with_lead_in(_wav_ms(4800), 0.5)
        with wave.open(io.BytesIO(out), "rb") as reader:
            self.assertEqual(reader.getframerate(), 48000)
            self.assertEqual(reader.getnchannels(), 1)
            self.assertEqual(reader.getnframes(), 4800 + 24000)

    def test_bad_url_refused(self) -> None:
        import os

        os.environ["SPEAKER_NAME"] = "test-speaker"
        import speaker_fixture

        body, _extra = speaker_fixture.build_body()
        with self.assertRaises(Exception) as ctx:
            asyncio.run(body._handlers["play"](url="http://x/evil.wav"))
        self.assertIn("/api/voice/", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
