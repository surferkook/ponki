"""The house voice's words: MMS-TTS reads only letters, so numbers, units
and symbols become words, and punctuation becomes pauses (spec 06).

Pure text handling: no model is loaded.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "voice"))

import voice_server as voice  # noqa: E402  (the model loads in a thread; not awaited here)


class Words(unittest.TestCase):
    def test_numbers(self) -> None:
        self.assertEqual(voice.number_words(0), "zero")
        self.assertEqual(voice.number_words(40), "forty")
        self.assertEqual(voice.number_words(101), "one hundred one")
        self.assertEqual(voice.number_words(2500), "two thousand five hundred")
        self.assertEqual(voice.number_words(1_000_000), "one million")

    def test_units_and_symbols(self) -> None:
        said = voice.to_words("Turn 30° and roll 40 cm, 70% sure & 3x faster.")
        self.assertEqual(said, "Turn thirty degrees and roll forty centimeters, "
                               "seventy percent sure and three times faster.")
        self.assertEqual(voice.to_words("1 m"), "one meter")
        self.assertEqual(voice.to_words("12.5 s"), "twelve point five seconds")

    def test_money_and_versions(self) -> None:
        self.assertEqual(voice.to_words("$0.003"), "zero point zero zero three dollars")
        self.assertEqual(voice.to_words("v 1.0.0"), "v one point zero point zero")

    def test_no_digit_survives(self) -> None:
        said = voice.to_words("Room 7, 8 robots, 9 lives, 1999, 2,500 ms.")
        self.assertFalse(any(ch.isdigit() for ch in said), said)

    def test_pauses(self) -> None:
        self.assertEqual(voice.phrases("Hi, I'm Buddy. Ready?"),
                         [("Hi", 0.14), ("I'm Buddy", 0.32), ("Ready", 0.0)])
        self.assertEqual(voice.phrases("..."), [])


class Presets(unittest.TestCase):
    def test_hub_and_service_agree(self) -> None:
        from hub import voice as hub_voice
        self.assertEqual({v["id"] for v in hub_voice.VOICES}, set(voice.PRESETS))
        for v in hub_voice.VOICES:
            self.assertEqual(v["label"], voice.PRESETS[v["id"]][0])


if __name__ == "__main__":
    unittest.main()
