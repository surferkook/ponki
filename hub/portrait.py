"""Portraits and drawings (spec 03 §4): Muse Image paints, the brain art-directs."""
from __future__ import annotations

import base64
import io
import os
import re
import time

from . import apilog, voice

STYLE_LINE = (
    "A square character portrait for a profile picture, the robot centred with "
    "margin so it fits a circle, friendly flat illustration with bold dark "
    "outlines, a few clean colours, soft shading, big expressive eyes or an "
    "eye-like feature, a plain flat pale background in its signature colour, "
    "nothing else in the scene, no text, letters or logos."
)

LOOK_PROMPT = (
    "You art-direct a home robot's portrait. From its name, body, persona and "
    "abilities, answer with exactly these tags and nothing else:\n"
    "<color>#RRGGBB</color> — a saturated signature colour that fits its "
    "personality, readable on white and on dark navy (no near-black, near-white or grey).\n"
    "<tagline>…</tagline> — at most 8 words, in its own voice, how it introduces itself.\n"
    "<voice>id</voice> — one voice id from the list given.\n"
    "<pitch>1.0</pitch> — 1.0 is natural and for big robots; up to 1.4 for a tiny robot; never below 1.0.\n"
    "<look>…</look> — 2–3 concrete sentences for an illustrator on how this exact "
    "body looks as a character: its real parts, shapes and colours, an expression "
    "and pose for this personality. No names, no text in the image."
)


def parse_look(text: str) -> dict:
    def tag(name: str) -> str:
        match = re.search(rf"<{name}>(.*?)</{name}>", text or "", re.S)
        return match.group(1).strip() if match else ""

    colour = tag("color").lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", colour):
        colour = "#4a6cf7"
    try:
        pitch = float(tag("pitch") or 1.0)
    except ValueError:
        pitch = 1.0
    return {"color": colour, "tagline": tag("tagline")[:80],
            "voice_id": tag("voice"), "pitch": pitch, "look": tag("look")}


def _resize(raw: bytes, side: int, fmt: str) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img.load()
    img = img.convert("RGB")
    img.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "WEBP":
        img.save(buf, format="WEBP", quality=88)
    else:
        img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


async def paint(name: str, picture: str, side: int = 512, fmt: str = "WEBP",
                label: str = "portrait", client=None) -> bytes:
    """Paint one picture with Muse Image. Returns image bytes at `side` px.

    One function for portraits (512 px WebP) and drawings (1024 px JPEG).
    A safety block (400 content_policy_violation) is not retried and not
    billed; a 503 is transient and retried once.
    """
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url="https://api.meta.ai/v1",
                             api_key=os.environ.get("META_API_KEY") or "not-set-yet")
    prompt = picture if label != "portrait" else f"{picture} {STYLE_LINE}"
    started = time.time()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.images.generate(
                model="muse-image-1.0", prompt=prompt, size="1024x1024", n=1,
                response_format="b64_json", extra_body={"output_format": "png"})
            break
        except Exception as exc:
            last_exc = exc
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if status == 503 and attempt == 0:
                continue
            apilog.write({"t": time.time(), "kind": "image", "who": f"{label} {name}",
                          "error": f"{type(exc).__name__}: {exc}"})
            raise
    else:
        raise last_exc or RuntimeError("image generation failed")
    raw = base64.b64decode(response.data[0].b64_json)
    apilog.write({"t": time.time(), "kind": "image", "who": f"{label} {name}",
                  "ms": int((time.time() - started) * 1000)})
    return _resize(raw, side, fmt)
