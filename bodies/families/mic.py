"""The mic family: a USB microphone that becomes the house microphone.

Found over USB (any USB device with a sound capture node), it needs no
connect step: plugged in is ready. The driver is the mic fixture
(bodies/mic/mic_fixture.py), which listens through it for the house.
Heavy imports (PyAV, NumPy) live inside check, never at module level.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import usb as usb_lib  # noqa: E402

NAME = "mic"
TITLE = "USB microphone"
SCAN = "microphones"
FIXTURE = "mic"
CONNECT = {"label": "", "hint": "", "fields": []}
CHECK_HINT = "Clap or speak while it listens — it records about three seconds."
PLAN_PARAMS: dict = {}
EXAMPLE = {
    "device": {"id": "mic:BRIO", "name": "BRIO 4K Stream Edition",
               "source": "alsa:BRIO", "ready": True, "status": "plugged in",
               "usb": "1-1"},
    "plan": {"name": "house-mic", "description": "The house microphone.",
             "personality": "a quiet listener"},
}

GUIDE = """Any USB microphone plugged into this workshop (including the mic in a
USB webcam) becomes the house microphone: the house listens through it and
transcribes what it hears. There is nothing to connect or set up — plugged
in means ready. Run the hearing check in the room where it will live, and
make a little noise (clap, speak) so everyone can hear that it works."""


def setup(config: dict, device: dict) -> dict:
    source = device.get("source")
    if not source:
        device_id = str(device.get("id") or "")
        if device_id.startswith("mic:"):
            source = f"alsa:{device_id[len('mic:'):]}"
    if source:
        config["source"] = source
    if device.get("name"):
        config["name"] = device["name"]
    config["device"] = dict(device)
    return config


def plan(config: dict, args: dict) -> str:
    return ""


def checks(config: dict, device: dict) -> dict:
    return {
        "hearing": {
            "title": "hearing",
            "steps": ["Make a little noise — clap or say something — "
                      "while it listens for about three seconds."],
            "sig": "hearing",
        },
    }


def problems(config: dict):
    device = config.get("device") or {}
    source = config.get("source") or device.get("source")
    if not source:
        return (["the microphone has no source yet"], [])
    return ([], [])


def body(config: dict, device: dict) -> dict:
    name = (device or {}).get("name") or "USB microphone"
    source = (device or {}).get("source") or config.get("source") or ""
    return {"head": ["Part", "What it is"],
            "rows": [["microphone", f"{name} · {source}"]]}


def discover(taken=None) -> list[dict]:
    taken = taken or {}
    # ALSA card number -> card id (e.g. "1" -> "BRIO") from /proc/asound/cards.
    card_ids: dict[str, str] = {}
    try:
        cards_text = Path("/proc/asound/cards").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        cards_text = ""
    for line in cards_text.splitlines():
        match = re.match(r"\s*(\d+)\s+\[\s*([^\]]+?)\s*\]:", line)
        if match:
            card_ids[match.group(1)] = match.group(2).strip()
    out: list[dict] = []
    seen: set[str] = set()
    for usb_device in usb_lib.devices():
        for node in usb_device.get("nodes") or []:
            if not isinstance(node, dict) or node.get("kind") != "sound":
                continue
            number = str(node.get("card") or "")
            if not number:
                continue
            card_id = card_ids.get(number, number)
            device_id = f"mic:{card_id}"
            if device_id in seen:
                continue
            seen.add(device_id)
            device = {"id": device_id, "name": usb_lib.label(usb_device),
                      "ready": True, "status": "plugged in",
                      "usb": usb_device.get("path"),
                      "source": f"alsa:{card_id}", "family": NAME}
            owner = taken.get(device_id) if isinstance(taken, dict) else None
            if owner:
                device.update(ready=False, robot=owner, status="in use")
            out.append(device)
    return out


def connect(device: dict, fields: dict):
    return ("the microphone is plugged in and ready", device)


async def check(device: dict, config: dict, check_id: str) -> dict:
    import asyncio

    import av
    import numpy as np

    if check_id != "hearing":
        raise ValueError(f"unknown check '{check_id}'")
    source = (config or {}).get("source") or (device or {}).get("source") or ""
    if not source:
        device_id = str((device or {}).get("id") or "")
        if device_id.startswith("mic:"):
            source = f"alsa:{device_id[len('mic:'):]}"

    def blocking() -> dict:
        import base64
        import io
        import wave

        if source.startswith("alsa:"):
            card = source[len("alsa:"):]
            container = av.open(f"hw:CARD={card}", format="alsa")
        else:
            container = av.open(source)
        try:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono",
                                          rate=16000)
            chunks: list = []
            total = 0
            target = 16000 * 3  # about three seconds at 16 kHz
            for frame in container.decode(stream):
                for out_frame in resampler.resample(frame):
                    samples = out_frame.to_ndarray().reshape(-1).astype(np.int16)
                    chunks.append(samples)
                    total += len(samples)
                if total >= target:
                    break
            audio = np.concatenate(chunks)[:target] if chunks \
                else np.zeros(0, dtype=np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(audio.astype("<i2").tobytes())
            return {"expect": "you should hear the room (the house transcribes it)",
                    "audio": base64.b64encode(buf.getvalue()).decode("ascii")}
        finally:
            container.close()

    return await asyncio.to_thread(blocking)


def command(config_path: str, config: dict):
    driver = Path(__file__).parent.parent / "mic" / "mic_fixture.py"
    device = config.get("device") or {}
    source = config.get("source") or device.get("source") or ""
    if not source:
        device_id = str(device.get("id") or "")
        if device_id.startswith("mic:"):
            source = f"alsa:{device_id[len('mic:'):]}"
    name = config.get("name") or config.get("display_name") \
        or device.get("name") or ""
    return ([sys.executable, "-u", str(driver)],
            {"MIC_SOURCE": str(source), "MIC_NAME": str(name)})
