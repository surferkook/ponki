"""The house voice: text to 48 kHz speech with Meta's MMS-TTS (spec 06).

    uvicorn voice_server:app --host 127.0.0.1 --port 8202

Every model Ponki calls at runtime is a Meta model, so the house speaks with
MMS-TTS (facebook/mms-tts-eng, CC-BY-NC 4.0), run locally on the CPU. It has
one English speaker; the house's voices are presets of it, each with its own
pitch and pace, so robots still sound like themselves.

MMS reads only lowercase letters (and a few digits), with no punctuation, so
the text is spoken in words first (numbers, units, symbols) and synthesised a
phrase at a time, with short pauses where the punctuation was. Speed and pitch
are done with a single polyphase resample: synthesis runs at speed/pitch, then
the clip is resampled by 48000/16000/pitch, so the pitch moves and the pace
stays. Everything leaves as 48 kHz mono 16-bit PCM.
"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import wave
from fractions import Fraction

import numpy as np
from fastapi import FastAPI, HTTPException, Response

MODEL = "facebook/mms-tts-eng"
REVISION = os.environ.get("MMS_REVISION", "c71de0fe7204c83f1c10820a7d696d0b450048ba")
PEAK = 0.7
OUT_RATE = 48000
SEED = 7  # MMS samples its prosody: a fixed seed keeps each voice the same every time

# id: (label, description, pitch, pace)
PRESETS = {
    "warm": ("Warm (MMS): steady, friendly", "steady, friendly voice", 1.0, 1.0),
    "bright": ("Bright (MMS): lively, clear", "bright, lively voice", 1.15, 1.05),
    "small": ("Small (MMS): little, quick", "small, quick voice", 1.32, 1.1),
    "low": ("Low (MMS): deep, calm", "low, calm voice", 0.86, 0.95),
    "playful": ("Playful (MMS): cheeky, bouncy", "playful, cheeky voice", 1.08, 1.15),
    "soft": ("Soft (MMS): gentle, unhurried", "soft, gentle voice", 1.05, 0.9),
}
VOICES = {k: v[0] for k, v in PRESETS.items()}
_FIRST = "warm"

app = FastAPI(title="ponkihouse voice")
_state = {"state": "loading", "error": None}
_lock = threading.Lock()
_mms: dict = {}


# -- text into words MMS can say (pure) ------------------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_UNITS = {"cm": "centimeters", "mm": "millimeters", "km": "kilometers", "m": "meters",
          "kg": "kilograms", "°": "degrees", "%": "percent", "x": "times",
          "s": "seconds", "ms": "milliseconds", "h": "hours", "min": "minutes"}
_SYMBOLS = {"&": " and ", "+": " plus ", "=": " equals ", "@": " at ", "#": " number ",
            "€": " euros ", "$": " dollars ", "/": " ", "°": " degrees ", "%": " percent "}


def number_words(n: int) -> str:
    """0 to 999,999,999 in words; bigger numbers digit by digit."""
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + number_words(rest))
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        if size <= n < size * 1000:
            rest = n % size
            return number_words(n // size) + " " + name + \
                ("" if rest == 0 else " " + number_words(rest))
    return " ".join(_ONES[int(d)] for d in str(n))


def _say_number(match: re.Match) -> str:
    money, whole, frac, unit = match.group(1), match.group(2), match.group(3), match.group(4)
    words = number_words(int(whole.replace(",", "")))
    if frac:
        words += " point " + " ".join(_ONES[int(d)] for d in frac)
    if unit:
        unit_word = _UNITS.get(unit.lower(), unit)
        if words == "one" and unit_word.endswith("s") and unit_word != "times":
            unit_word = unit_word[:-1]
        words += " " + unit_word
    if money:
        words += " dollars" if money == "$" else " euros"
    return f" {words} "


def _say_version(match: re.Match) -> str:
    return " " + " point ".join(number_words(int(part)) for part in match.group(0).split(".")) + " "


_NUMBER = re.compile(
    r"(?<![\w.])([$€])?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(cm|mm|km|kg|ms|min|m|s|h|x|°|%)?(?!\w)")
_VERSION = re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w])")


def to_words(text: str) -> str:
    """Numbers, units and symbols as words; punctuation kept for the pauses."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", ", ").replace("–", ", ").replace("…", ". ")
    text = _VERSION.sub(_say_version, text)
    text = _NUMBER.sub(_say_number, text)
    for symbol, words in _SYMBOLS.items():
        text = text.replace(symbol, words)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def phrases(text: str) -> list[tuple[str, float]]:
    """(phrase, pause after it in seconds): longer after a sentence, shorter at a comma."""
    out = []
    for piece, mark in re.findall(r"([^.!?;:,]+)([.!?;:,]*)", text):
        piece = piece.strip()
        if not re.search(r"[A-Za-z]", piece):
            continue
        pause = 0.32 if re.search(r"[.!?]", mark) else 0.14 if mark else 0.0
        out.append((piece, pause))
    if out:
        out[-1] = (out[-1][0], 0.0)
    return out


# -- MMS ----------------------------------------------------------------------------

def _load() -> None:
    try:
        import torch
        from transformers import AutoTokenizer, VitsModel

        torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))
        _mms["tokenizer"] = AutoTokenizer.from_pretrained(MODEL, revision=REVISION)
        _mms["model"] = VitsModel.from_pretrained(MODEL, revision=REVISION).eval()
        _mms["rate"] = int(_mms["model"].config.sampling_rate)
        _synth("Ready.", 1.0)  # warm-up
        _state.update(state="ready", error=None)
    except Exception as exc:
        _state.update(state="error", error=f"{type(exc).__name__}: {exc}")


def _synth(text: str, rate: float) -> np.ndarray:
    """One phrase at MMS's own sample rate; rate > 1 is faster."""
    import torch

    model, tokenizer = _mms["model"], _mms["tokenizer"]
    inputs = tokenizer(text, return_tensors="pt")
    if inputs["input_ids"].shape[-1] == 0:
        return np.zeros(0, dtype=np.float32)
    model.speaking_rate = rate
    torch.manual_seed(SEED)
    with torch.no_grad():
        return model(**inputs).waveform[0].numpy().astype(np.float32)


def speak_text(text: str, rate: float) -> tuple[np.ndarray, int]:
    native = _mms["rate"]
    chunks = []
    for piece, pause in phrases(to_words(text)):
        audio = _synth(piece, rate)
        if len(audio):
            chunks.append(audio)
            if pause:
                chunks.append(np.zeros(int(native * pause / rate), dtype=np.float32))
    if not chunks:
        raise ValueError("there were no words to say")
    return np.concatenate(chunks), native


def _shape(audio: np.ndarray, native: int, pitch: float) -> bytes:
    """One resample does rate conversion + pitch; fades; peak; 16-bit WAV."""
    from scipy.signal import resample_poly

    frac = Fraction(OUT_RATE, native) / Fraction(pitch).limit_denominator(1000)
    frac = frac.limit_denominator(2000)
    out = resample_poly(audio, frac.numerator, frac.denominator).astype(np.float32)
    fade = min(240, len(out) // 2)
    if fade:
        out[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0:
        out *= PEAK / peak
    pcm = (np.clip(out, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUT_RATE)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


# -- the service ----------------------------------------------------------------------

@app.get("/health")
def health():
    return {"state": _state["state"], "error": _state["error"], "model": MODEL}


@app.get("/voices")
def voices():
    return dict(VOICES)


@app.post("/speak")
def speak(payload: dict):
    if _state["state"] != "ready":
        detail = "voice is loading" if _state["state"] == "loading" \
            else f"voice is error: {_state['error']}"
        raise HTTPException(503, detail)
    text = (payload.get("text") or "").strip()[:1000]
    if not text:
        raise HTTPException(400, "empty text")
    voice_id = payload.get("voice") or _FIRST
    if voice_id not in PRESETS:
        raise HTTPException(400, f"unknown voice '{voice_id}'")
    _label, _desc, base_pitch, base_pace = PRESETS[voice_id]
    speed = min(1.6, max(0.6, float(payload.get("speed", 1.0)))) * base_pace
    pitch = min(1.6, max(0.7, float(payload.get("pitch", 1.0)) * base_pitch))
    started = time.time()
    with _lock:  # one synthesis at a time
        try:
            audio, native = speak_text(text, speed / pitch)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        wav = _shape(audio, native, pitch)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        duration_ms = int(reader.getnframes() / OUT_RATE * 1000)
    return Response(wav, media_type="audio/wav", headers={
        "X-Synth-Ms": str(int((time.time() - started) * 1000)),
        "X-Duration-Ms": str(duration_ms)})


threading.Thread(target=_load, daemon=True).start()
