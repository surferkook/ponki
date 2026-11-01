"""RoBoHoN's body (spec 08): a thin driver over his own control server.

His server (a separate app on the NUC, http://localhost:8091) already talks
to the robot; we only call its HTTP API — never /api/chat or /api/listen
(his server has its own brain), never raw motion or servo endpoints.

His voice ("voice" in his config, or HOUSE_ROBOHON_VOICE):
  house   (default) the house voice speaks for him, on the house speakers:
          every model Ponki calls stays a Meta model
  stream  the house voice, played on his own speaker: his server must offer
          POST /api/play_wav (a WAV body); it says so plainly if it doesn't
  own     his server's own voices (not Meta models: Kokoro, Piper, StyleTTS2)

Run: python -u bodies/robohon/robohon_body.py [data/robots/robohon.json]
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))

import httpx  # noqa: E402

from body import Body, Image  # noqa: E402

BODY = ("A 20 cm Sharp RoBoHoN humanoid: walks, dances, waves, sings, LED eyes, "
        "a camera in his forehead, a speaker and a mic. When docked in his "
        "charging cradle his legs are locked — only upper-body gestures work there.")

DUTCH = set("ik je jij het een niet wat dat is ben hoi hallo goed dank leuk mooi "
            "wel ook maar zijn heb hebt naar voor vandaag lekker".split())
WALK_DESC = "walk forward a few steps (about 10 cm: three small steps)"
LEGS = {"cradle": "locked: docked in the cradle, upper-body gestures only",
        "stand": "free: you can walk"}
MOTION_RE = re.compile(r"startMotion\(.*?\)\s*ret=(-?\d+)")


def pick_voice(text: str) -> str:
    if any("Ѐ" <= ch <= "ӿ" for ch in text):
        return "styletts2:dzambazov"
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if words and sum(w in DUTCH for w in words) / len(words) > 0.15:
        return "nl_NL-alex-medium"
    return "kokoro:af_heart"


VOICE_MODES = ("house", "stream", "own")


def voice_mode(config: dict) -> str:
    """house (default), stream or own; anything else is house."""
    mode = str(config.get("voice") or os.environ.get("HOUSE_ROBOHON_VOICE") or "house")
    return mode if mode in VOICE_MODES else "house"


def split_lines(text: str, limit: int = 300) -> list[str]:
    """Split at sentence ends into pieces of at most `limit` chars."""
    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:  # one very long sentence: cut at a word
            cut = sentence.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current)
    return [p for p in out if p]


def wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data)) as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:
        return 0.0


def legs_for(pose: str) -> str:
    return LEGS.get(pose, "not standing")


class Robohon:
    def __init__(self, web: str) -> None:
        self.web = web.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)
        self.speak_lock = asyncio.Lock()

    async def gestures(self) -> list[dict]:
        resp = await self.client.get(f"{self.web}/api/gestures")
        resp.raise_for_status()
        return resp.json()

    async def play_wav(self, wav: bytes) -> str:
        """Play a house-voice clip on his own speaker (voice: stream)."""
        async with self.speak_lock:
            resp = await self.client.post(f"{self.web}/api/play_wav", content=wav,
                                          headers={"Content-Type": "audio/wav"})
            if resp.status_code in (404, 405):
                raise ValueError("his server can't play house audio yet "
                                 "(no /api/play_wav): set his voice to house")
            resp.raise_for_status()
            await asyncio.sleep(0.8 + wav_seconds(wav))  # he fetches it over Wi-Fi, then plays
        return "played"

    async def speak(self, text: str) -> str:
        async with self.speak_lock:  # his server is single-robot, one rb.wav
            for piece in split_lines(text):
                resp = await self.client.post(
                    f"{self.web}/api/speak",
                    json={"text": piece, "voice": pick_voice(piece), "gesture": ""})
                resp.raise_for_status()
                wait = 0.8  # he fetches the clip over Wi-Fi before playing
                try:
                    clip = await self.client.get(f"{self.web}/audio/rb.wav")
                    wait += wav_seconds(clip.content)
                except Exception:
                    pass
                await asyncio.sleep(wait)
        return "spoken"

    async def _log_lines(self) -> list[str]:
        try:
            resp = await self.client.get(f"{self.web}/api/devicelog")
            lines = resp.text.splitlines()
        except Exception:
            return []
        return [ln for ln in lines
                if "ROBOHON-POSE" not in ln and "PoseService" not in ln]

    async def _confirm_motion(self, mark: str) -> int | None:
        """The first startMotion ret code after the log line `mark`."""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            lines = await self._log_lines()
            start = 0
            for i in range(len(lines) - 1, -1, -1):  # last sighting of the mark
                if lines[i] == mark:
                    start = i + 1
                    break
            for line in lines[start:]:
                match = MOTION_RE.search(line)
                if match:
                    return int(match.group(1))
            await asyncio.sleep(0.25)
        return None

    async def gesture(self, name: str) -> str:
        deadline = time.monotonic() + 15.0
        while True:
            lines = await self._log_lines()
            mark = lines[-1] if lines else ""
            resp = await self.client.post(f"{self.web}/api/gesture/{name}")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("reason") or "gesture didn't play")
            code = await self._confirm_motion(mark)
            if code is None or code == 0:
                return f"playing {name}"
            if code == -4:
                if time.monotonic() >= deadline:
                    raise RuntimeError("still busy with the last move after 15 s")
                await asyncio.sleep(0.8)
                continue
            if code == -6:
                raise RuntimeError("the USB cable is plugged in: it blocks all motion")
            if code == -10:
                raise RuntimeError("not possible in this pose")
            raise RuntimeError(f"his body refused it (code {code})")

    async def look(self) -> Image:
        for _ in range(2):
            resp = await self.client.post(f"{self.web}/api/look")
            resp.raise_for_status()
            if resp.json().get("ok"):
                break
        else:
            raise RuntimeError("camera didn't return a frame (tried twice)")
        photo = await self.client.get(f"{self.web}/vision/latest.jpg")
        photo.raise_for_status()
        return Image(photo.content, "photo from your forehead camera")

    async def pose(self) -> dict:
        resp = await self.client.get(f"{self.web}/api/pose")
        resp.raise_for_status()
        return resp.json()


async def fetch_gestures(web: str) -> list[dict]:
    """The server may still be starting: retry 2 s → 30 s, never crash-loop."""
    delay = 2.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{web}/api/gestures")
                resp.raise_for_status()
                return resp.json()
        except Exception:
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)


def main() -> None:
    config: dict = {}
    if len(sys.argv) > 1:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    web = (config.get("web") or os.environ.get("HOUSE_ROBOHON_WEB")
           or "http://localhost:8091").rstrip("/")
    name = config.get("name") or "robohon"
    gestures = asyncio.run(fetch_gestures(web))
    robot = Robohon(web)

    keys = [g["key"] for g in gestures if g.get("key")]
    menu = "; ".join(
        f"{g['key']}: {WALK_DESC if g['key'] == 'walk' else g.get('desc', '')}"
        for g in gestures if g.get("key"))

    mode = voice_mode(config)
    body = Body(name, config.get("description") or BODY,
                voice="speak" if mode == "own" else None,
                voice_play="play" if mode == "stream" else None,
                display_name=config.get("display_name") or "RoBoHoN",
                personality=config.get("personality"))

    if mode == "own":
        @body.action("speak", "Say a line out loud.",
                     {"text": {"type": "string"}}, sound=True)
        async def speak(text: str):
            return await robot.speak(text)

    if mode == "stream":
        @body.action("play", "Play a house-voice clip on his own speaker.",
                     {"url": {"type": "string"}}, sound=True)
        async def play(url: str):
            return await robot.play_wav(await body.house_clip(url))

    @body.action("gesture",
                 "Perform one body gesture (head/arms/legs/eye-lights) that fits "
                 "the moment. Fire it BEFORE you reply so it plays while you speak. "
                 "In the cradle only upper-body gestures work. Getting somewhere "
                 "takes many small moves: one move, then look (or locate) to check "
                 f"where you are, then the next; a walk covers about 10 cm. Options — {menu}",
                 {"name": {"type": "string", "enum": keys}})
    async def gesture(name: str):
        return await robot.gesture(name)

    @body.action("look", "Take a photo through your forehead camera.",
                 {}, returns="image")
    async def look():
        return await robot.look()

    async def pose_loop() -> None:
        while True:
            try:
                data = await robot.pose()
                pose = str(data.get("pose", ""))
                if pose:
                    await body.set_state(pose=pose, legs=legs_for(pose))
            except Exception:
                pass  # his server restarting; try again next round
            await asyncio.sleep(3.0)

    body.run(pose_loop())


if __name__ == "__main__":
    main()
