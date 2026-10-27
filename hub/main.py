"""The hub: FastAPI app, /ws/body, /ws/ui, the REST API, the web UI (spec 02).

Run: .venv/bin/uvicorn hub.main:app --host 0.0.0.0 --port 8200
Loads .env from the repo root before importing the hub modules.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import struct
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

from . import apilog  # noqa: E402
from .builder import Builder, family_catalog  # noqa: E402
from .export import export_media, format_event_line  # noqa: E402
from .house import House  # noqa: E402
from .imaging import for_model  # noqa: E402
from . import pricing as pricing_mod  # noqa: E402
from . import voice as voice_mod  # noqa: E402
from .providers import get_provider  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
CSP = {"Content-Security-Policy": "default-src 'none'; sandbox"}
STATIC = Path(__file__).parent / "static" / "index.html"

VOICES = [
    {"id": "heart", "label": "Heart (Kokoro): warm, friendly woman"},
    {"id": "bella", "label": "Bella (Kokoro): bright, lively woman"},
    {"id": "nicole", "label": "Nicole (Kokoro): soft, breathy, whispery woman"},
    {"id": "sky", "label": "Sky (Kokoro): light, young woman"},
    {"id": "michael", "label": "Michael (Kokoro): warm, mature man"},
    {"id": "puck", "label": "Puck (Kokoro): playful, cheeky man"},
    {"id": "amy", "label": "Amy (Piper): warm, clear woman"},
    {"id": "hfc", "label": "HFC (Piper): bright, crisp woman"},
    {"id": "ryan", "label": "Ryan (Piper): clear, friendly man"},
]


def _preview_wav(pitch: float = 1.0, speed: float = 1.0) -> bytes:
    rate = 22050
    seconds = max(0.3, 0.9 / max(speed, 0.1))
    frames = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        data = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * pitch * i / rate)
                                  * (1 - i / frames)))
            for i in range(frames))
        wav.writeframes(data)
    return buf.getvalue()


async def _services(house: House) -> dict:
    """Service health in the UI's shape: {ready, state, detail, model}."""
    from . import grounding as grounding_mod
    from . import hearing as hearing_mod

    voice_h = await voice_mod.health()
    ears_h = await hearing_mod.health()
    find_h = await grounding_mod.health()
    return {
        "voice": {"ready": voice_h.get("state") == "ready",
                  "state": voice_h.get("state"),
                  "detail": house.voice_error or voice_h.get("error") or "",
                  "speakers": sorted(house.fixtures_of("speaker"))},
        "ears": {"ready": ears_h.get("state") == "ready",
                 "state": ears_h.get("state"),
                 "detail": ears_h.get("error") or "",
                 "model": ears_h.get("model")},
        "finding": {"ready": find_h.get("state") == "ready",
                    "state": find_h.get("state"),
                    "detail": find_h.get("error") or "",
                    "model": find_h.get("model")},
    }


def _fingerprint_sweep(house: House) -> None:
    """Background: label any kept photo that has no fingerprint label yet."""
    import base64

    try:
        labels = apilog.label_index()
        seen = house.data_dir / "seen"
        if not seen.exists():
            return
        for robot_dir in seen.iterdir():
            if not robot_dir.is_dir():
                continue
            for jpg in robot_dir.glob("*.jpg"):
                if not jpg.stem.isdigit():
                    continue
                if f"{robot_dir.name} #{jpg.stem}" in labels:
                    continue
                model = for_model(base64.b64encode(jpg.read_bytes()).decode("ascii"))
                apilog.register(model, f"{robot_dir.name} #{jpg.stem}")
    except Exception:
        pass


def create_app(data_dir: Path | None = None, token: str | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("HOUSE_DATA_DIR") or (_REPO / "data"))
    house_token = os.environ.get("HOUSE_TOKEN", "") if token is None else token
    if not house_token:
        print("WARNING: HOUSE_TOKEN is empty — any body may connect (dev only)")
    try:
        provider = get_provider("")
    except ValueError as exc:  # a contributor model or a bad effort fails at start-up
        raise SystemExit(f"cannot start the hub: {exc}")
    house = House(provider, os.environ.get("HOUSE_HUMAN", "your human"), data_dir)
    builder = Builder(house)
    house.builder = builder  # type: ignore[attr-defined]
    threading.Thread(target=_fingerprint_sweep, args=(house,), daemon=True).start()

    app = FastAPI(title="Ponki")
    app.state.house = house
    app.state.builder = builder
    app.state.token = house_token

    def _robot(rid: str) -> dict:
        if rid not in house.manifests:
            raise HTTPException(404, f"no robot '{rid}'")
        return house.manifests[rid]

    def _robot_info(rid: str) -> dict:
        man = house.manifest(rid)
        record = house.registry.get(rid)
        link = house.bodies.get(rid)
        actions = man.get("actions") or {}
        camera = next((name for name, a in actions.items()
                       if isinstance(a, dict) and a.get("returns") == "image"), None)
        nums = house._photo_numbers(rid)
        info = {
            "name": rid,
            "online": link is not None,
            "since": link.since if link else None,
            "display_name": (record or {}).get("display_name") or man.get("display_name") or rid,
            "description": man.get("description", ""),
            "voice": man.get("voice"),
            "voice_style": man.get("voice_style"),
            "actions": actions,
            "camera": camera,
            "state": dict(link.state) if link else {},
            "seen": f"/api/robots/{rid}/seen/{nums[-1]}.jpg" if nums else None,
            "record": record,
        }
        info.update(house.soul(rid).info())
        return info

    # -- the page ---------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC, media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/info")
    async def info():
        found = pricing_mod.price_for(house.model)
        price = ({"in": found[0], "out": found[1], "cached": found[2]} if found else None)
        return {"provider": house.provider_name, "model": house.model,
                "human": house.human, "price": price,
                "ui": STATIC.stat().st_mtime if STATIC.exists() else 0,
                "voice": {**await voice_mod.health(),
                          "speakers": sorted(house.fixtures_of("speaker"))}}

    @app.get("/api/robots")
    async def robots():
        return [_robot_info(rid) for rid in sorted(house.manifests)]

    @app.get("/api/events")
    async def events():
        return list(house.events)

    # -- talking and acting -------------------------------------------------------
    @app.post("/api/robots/{rid}/chat")
    async def chat(rid: str, payload: dict):
        _robot(rid)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "say something first — the message was empty")
        house.emit("heard", rid, text=text, frm=house.human)
        try:
            reply = await house._run_turn(rid, text, "human")
        except Exception as exc:
            raise HTTPException(502, f"brain error: {exc}")
        return {"reply": reply}

    @app.post("/api/robots/{rid}/act")
    async def do_act(rid: str, payload: dict):
        _robot(rid)
        result = await house.act(rid, payload.get("action", ""), payload.get("args") or {})
        return {"ok": result["ok"], "text": result["text"]}

    @app.post("/api/robots/{rid}/notice")
    async def notice(rid: str, payload: dict):
        _robot(rid)
        text = (payload.get("text") or "").strip()
        house.emit("noticed", rid, text=text, staged=True)
        house.spawn(house._run_turn(rid, text, "event"))
        return {"ok": True}

    @app.post("/api/robots/{rid}/locate")
    async def locate(rid: str, payload: dict):
        _robot(rid)
        result = await house.locate(rid, payload.get("what") or "")
        return {"ok": result["ok"], "text": result["text"]}

    @app.post("/api/robots/{rid}/watch")
    async def watch(rid: str, payload: dict):
        _robot(rid)
        result = await house.watch(rid, payload.get("what") or "",
                                   payload.get("seconds"))
        return {"ok": result["ok"], "text": result["text"]}

    # -- photos ---------------------------------------------------------------------
    @app.get("/api/robots/{rid}/seen/{filename}")
    async def seen_file(rid: str, filename: str):
        match = re.fullmatch(r"(\d+)\.(jpg|gif|mp4)", filename or "")
        if not match:
            raise HTTPException(404, "that photo is gone (only the most recent ones are kept)")
        path = house.data_dir / "seen" / rid / filename
        if not path.exists():
            if match.group(2) == "jpg":
                raise HTTPException(404, "that photo is gone (only the most recent ones are kept)")
            raise HTTPException(404, "that clip is gone (only the most recent ones are kept)")
        media = {"jpg": "image/jpeg", "gif": "image/gif", "mp4": "video/mp4"}[match.group(2)]
        headers = {"Cache-Control": "max-age=86400"}
        if match.group(2) in ("gif", "mp4"):
            headers["Content-Disposition"] = f'inline; filename="{rid}-{filename}"'
        return FileResponse(path, media_type=media, headers=headers)

    @app.delete("/api/robots/{rid}/seen/{n}")
    async def delete_seen(rid: str, n: int):
        _robot(rid)
        try:
            return house.delete_photo(rid, n)
        except KeyError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/robots/{rid}/forget-unkept-photos")
    async def forget_unkept(rid: str):
        _robot(rid)
        return {"removed": house.forget_unkept_photos(rid)}

    # -- soul shaping -----------------------------------------------------------------
    @app.post("/api/robots/{rid}/rename")
    async def rename(rid: str, payload: dict):
        _robot(rid)
        try:
            new = await house.rename(rid, payload.get("display_name", ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"display_name": new}

    @app.get("/api/robots/{rid}/portrait.webp")
    async def portrait(rid: str):
        import base64 as _b64

        if rid not in house.manifests:
            return JSONResponse({"detail": "no portrait yet"}, status_code=404, headers=CSP)
        data = house.soul(rid).data.get("portrait")
        if not data:
            return JSONResponse({"detail": "no portrait yet"}, status_code=404, headers=CSP)
        return Response(_b64.b64decode(data), media_type="image/webp",
                        headers={**CSP, "Cache-Control": "max-age=86400"})

    @app.post("/api/robots/{rid}/portrait")
    async def draw_portrait(rid: str):
        _robot(rid)
        soul = house.soul(rid)
        if soul.data.get("painting"):
            raise HTTPException(409, "already drawing")
        house.spawn(soul.paint(house.manifest(rid)))
        return {"ok": True, "painting": True}

    @app.put("/api/robots/{rid}/persona")
    async def persona(rid: str, payload: dict):
        _robot(rid)
        text = (payload.get("persona") or "").strip()
        if len(text) < 10:
            raise HTTPException(400, "write at least a sentence about who this robot is")
        await house.soul(rid).set_persona(text[:4000])
        house.emit("soul_edited", rid)
        return {"ok": True}

    @app.post("/api/robots/{rid}/reset")
    async def reset(rid: str, payload: dict):
        _robot(rid)
        forget = bool(payload.get("forget"))
        await house.soul(rid).reset(forget=forget)
        house.emit("reset", rid, forget=forget)
        return {"ok": True}

    @app.delete("/api/robots/{rid}/promises/{n}")
    async def drop_promise(rid: str, n: int):
        _robot(rid)
        try:
            house.soul(rid).drop_promise(n)
        except IndexError:
            raise HTTPException(404, "no promise with that number")
        return {"ok": True}

    @app.put("/api/robots/{rid}/voice")
    async def set_voice(rid: str, payload: dict):
        _robot(rid)
        cleaned = voice_mod.clean({"id": payload.get("id", ""),
                                   "pitch": payload.get("pitch", 1.0),
                                   "speed": payload.get("speed", 1.0)})
        if cleaned is None:
            raise HTTPException(400, f"unknown voice '{payload.get('id', '')}'")
        house.soul(rid).set_voice(cleaned)
        house.emit("voice_changed", rid, voice=cleaned)
        return {"ok": True, "voice": cleaned}

    @app.post("/api/robots/{rid}/voice/preview")
    async def preview_voice(rid: str, payload: dict):
        _robot(rid)
        cleaned = voice_mod.clean({"id": payload.get("id", ""),
                                   "pitch": payload.get("pitch", 1.0),
                                   "speed": payload.get("speed", 1.0)})
        if cleaned is None:
            raise HTTPException(400, f"unknown voice '{payload.get('id', '')}'")
        soul = house.soul(rid)
        text = soul.data.get("tagline") or f"Hi, I'm {house.label(rid)}."
        try:
            wav, _ms = await voice_mod.synth(text, cleaned)
        except Exception as exc:
            raise HTTPException(503, str(exc))
        return Response(wav, media_type="audio/wav")

    @app.get("/api/voices")
    async def voices():
        return voice_mod.VOICES

    @app.get("/api/voice/{name}")
    async def voice_clip(name: str):
        match = re.fullmatch(r"(\d+)\.wav", name or "")
        path = house.data_dir / "voice" / name if match else None
        if not path or not path.exists():
            raise HTTPException(404, "that voice clip is gone")
        return FileResponse(path, media_type="audio/wav",
                            headers={"Cache-Control": "max-age=3600"})

    # -- goodbye / departed ---------------------------------------------------------
    @app.delete("/api/robots/{rid}")
    async def goodbye(rid: str, forget: bool = False):
        _robot(rid)
        record = house.registry.get(rid) or {}
        workshop = record.get("workshop")
        if workshop:
            if workshop not in house.workshops:
                raise HTTPException(409, f"{workshop}, the workshop that runs {rid}, "
                                         f"is offline: switch it on first")
            await house.act(f"@{workshop}", "stop_robot",
                            {"name": rid, "forget": True, "erase": forget}, timeout=20)
            for _ in range(20):
                if rid not in house.bodies:
                    break
                await asyncio.sleep(0.25)
        try:
            entry = house.remove(rid, forget=forget)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if forget:
            builder.forget_robot(rid)
            for sheet in (house.data_dir / "builds").glob("*.json"):
                try:
                    data = json.loads(sheet.read_text(encoding="utf-8"))
                    if data.get("name") == rid or data.get("robot") == rid:
                        sheet.unlink()
                except (ValueError, OSError):
                    pass
        return {"ok": True, "remembered": entry}

    @app.get("/api/departed")
    async def departed_list():
        out = []
        for entry in reversed(house.departed):
            item = dict(entry)
            item["portrait"] = (f"/api/departed/{entry['id']}/portrait.webp"
                                if entry.get("portrait") else None)
            out.append(item)
        return out

    @app.get("/api/departed/{entry_id}")
    async def departed_one(entry_id: str):
        try:
            entry, soul_data = house.remembrance(entry_id)
        except ValueError:
            raise HTTPException(404, "nobody by that id moved on")
        return {**entry, "persona": soul_data.get("persona", ""),
                "journal": soul_data.get("journal", []),
                "usage": soul_data.get("usage", {}),
                "conversation_len": len(soul_data.get("conversation") or [])}

    @app.post("/api/departed/{entry_id}/return")
    async def departed_return(entry_id: str):
        entry = next((e for e in house.departed if e.get("id") == entry_id), None)
        if entry is None:
            raise HTTPException(404, "nobody by that id moved on")
        workshop = entry.get("workshop")
        if workshop and workshop not in house.workshops:
            raise HTTPException(409, f"the workshop that ran {entry['name']} ({workshop}) "
                                     f"is offline — switch it on first")
        try:
            house.bring_back(entry_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        started = "its body has to be switched on again by hand"
        if workshop and workshop in house.workshops:
            result = await house.act(f"@{workshop}", "restore_robot",
                                     {"name": entry["name"]}, timeout=20)
            started = result["text"] if result["ok"] else \
                f"its workshop couldn't start it: {result['text']}"
        return {"name": entry["name"], "started": started}

    @app.delete("/api/departed/{entry_id}")
    async def departed_erase(entry_id: str):
        entry = next((e for e in house.departed if e.get("id") == entry_id), None)
        if entry is None:
            raise HTTPException(404, "nobody by that id moved on")
        workshop = entry.get("workshop")
        if workshop and workshop in house.workshops:
            await house.act(f"@{workshop}", "stop_robot",
                            {"name": entry["name"], "erase": True, "away": True}, timeout=20)
        house.erase_departed(entry_id)
        return {"ok": True}

    @app.get("/api/departed/{entry_id}/portrait.webp")
    async def departed_portrait(entry_id: str):
        import base64 as _b64

        try:
            _entry, soul_data = house.remembrance(entry_id)
        except ValueError:
            return JSONResponse({"detail": "no portrait kept"}, status_code=404, headers=CSP)
        data = soul_data.get("portrait")
        if not data:
            return JSONResponse({"detail": "no portrait kept"}, status_code=404, headers=CSP)
        return Response(_b64.b64decode(data), media_type="image/webp",
                        headers={**CSP, "Cache-Control": "max-age=86400"})

    # -- house settings, mic, system, logs --------------------------------------------
    @app.get("/api/house")
    async def get_house():
        room = [e for e in list(house.events) if e.get("kind") in ("say", "heard", "ask")][-12:]
        return {"settings": dict(house.settings), "room": room}

    @app.put("/api/house")
    async def put_house(payload: dict):
        if "mic" in payload and payload["mic"] is not None:
            mic = payload["mic"]
            link = house.fixtures.get(mic)
            if link is None or link.manifest.get("kind") != "mic":
                raise HTTPException(404, f"no mic called {mic}")
        for key in ("chatter", "always_on", "mic"):
            if key in payload:
                house.set_setting(key, payload[key])
        return {"settings": dict(house.settings)}

    @app.get("/api/mic")
    async def mic_state():
        from . import hearing as hearing_mod

        mics = []
        for mid, link in sorted(house.fixtures_of("mic").items()):
            record = house.registry.get(mid) or {}
            mics.append({"name": mid,
                         "display_name": record.get("display_name") or mid,
                         **link.state})
        return {"mics": mics, "house_mic": house.house_mic(),
                "always_on": bool(house.settings.get("always_on")),
                "ears": await hearing_mod.health()}

    @app.post("/api/mic/start")
    async def mic_start(payload: dict = {}):
        mic = house.house_mic()
        if mic is None:
            raise HTTPException(409, "no house mic is online")
        result = await house.act(f"@{mic}", "start", {}, timeout=10)
        if not result.get("ok"):
            raise HTTPException(502, result.get("text", "the mic failed"))
        return {"ok": True}

    @app.post("/api/mic/stop")
    async def mic_stop(payload: dict = {}):
        mic = house.house_mic()
        if mic is None:
            raise HTTPException(409, "no house mic is online")
        result = await house.act(f"@{mic}", "stop", {}, timeout=30)
        if not result.get("ok"):
            raise HTTPException(502, result.get("text", "the mic failed"))
        data = result.get("data") or {}
        heard = await house.heard_audio(
            mic, data.get("audio", ""), float(data.get("seconds") or 0),
            to=payload.get("to"), push=True)
        if heard.get("error"):
            raise HTTPException(502, heard["error"])
        return {"text": heard.get("text", ""), "to": heard.get("to"),
                "language": heard.get("language"), "seconds": heard.get("seconds"),
                "ms": heard.get("ms"), "engine": heard.get("engine")}

    @app.get("/api/system")
    async def system():
        workshops = []
        names = set(house.workshops) | {r["id"] for r in house.registry.all(kind="workshop")}
        names |= {r.get("workshop") for r in house.registry.all() if r.get("workshop")}
        for name in sorted(n for n in names if n):
            record = house.registry.get(name) or {}
            runs = [{"id": r["id"], "display_name": r.get("display_name"),
                     "kind": r.get("kind"), "family": r.get("family"),
                     "online": r["id"] in house.bodies or r["id"] in house.fixtures}
                    for r in house.registry.all() if r.get("workshop") == name]
            link = house.workshops.get(name)
            workshops.append({"id": name, "display_name": record.get("display_name") or name,
                              "online": name in house.workshops, "record": record, "runs": runs,
                              "families": list((link.state or {}).get("families") or [])
                              if link else []})
        equipment = []
        for rid, link in sorted(house.fixtures.items()):
            if link.manifest.get("kind") == "workshop":
                continue
            record = house.registry.get(rid) or {}
            equipment.append({"id": rid, "key": f"@{rid}", "kind": link.manifest.get("kind"),
                              "display_name": record.get("display_name") or rid,
                              "online": True, "record": record})
        for record in house.registry.all():
            if record.get("kind") in ("robot", "workshop"):
                continue
            if record["id"] not in house.fixtures:
                equipment.append({"id": record["id"], "key": f"@{record['id']}",
                                  "kind": record.get("kind"),
                                  "display_name": record.get("display_name"),
                                  "online": False, "record": record})
        return {
            "hub": {"provider": house.provider_name, "model": house.model,
                    "since": house.started, "robots": len(house.manifests)},
            "services": await _services(house),
            "settings": dict(house.settings),
            "house_mic": (await mic_state())["house_mic"],
            "workshops": workshops, "equipment": equipment,
        }

    @app.get("/api/logs")
    async def logs(source: str = "events", workshop: str = "", name: str = "",
                   unit: str = "", lines: int = 200, quiet: bool = True):
        lines = max(10, min(1000, lines))
        if source == "api":
            return {"lines": apilog.tail(lines)}
        if source == "workshop":
            target = workshop or name
            if target not in house.workshops:
                raise HTTPException(409, "that workshop is offline")
            result = await house.act(f"@{target}", "logs",
                                     {"name": name, "unit": unit, "lines": lines}, timeout=30)
            if not result["ok"]:
                raise HTTPException(502, result["text"])
            data = result.get("data")
            return {"lines": data if isinstance(data, list) else str(data).splitlines()}
        events = list(house.events)
        if quiet:
            events = [e for e in events if e.get("kind") not in ("voice", "state")]
        return {"lines": [format_event_line(e) for e in events[-lines:]]}

    @app.post("/api/equipment/{key}/rename")
    async def equipment_rename(key: str, payload: dict):
        try:
            new = house.rename_equipment(key, payload.get("display_name", ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"display_name": new}

    @app.delete("/api/equipment/{key}")
    async def equipment_delete(key: str):
        rid = key[1:] if key.startswith("@") else key
        record = house.registry.get(rid)
        if record is None or record.get("runner") != "workshop" \
                or record.get("kind") in ("robot", "workshop"):
            raise HTTPException(400, "only equipment added through Add a robot can be removed here")
        workshop = record.get("workshop")
        if workshop and workshop not in house.workshops:
            raise HTTPException(409, f"{workshop}, the workshop that runs {rid}, is offline")
        if workshop:
            await house.act(f"@{workshop}", "stop_robot", {"name": rid, "erase": True}, timeout=20)
        house.registry.pop(rid)
        if house.settings.get("mic") == rid:
            house.set_setting("mic", None)
        house.emit("equipment_removed", rid)
        return {"ok": True}

    @app.post("/api/export")
    async def do_export(payload: dict):
        since_raw = payload.get("since")
        if not since_raw:
            raise HTTPException(400, "say when the session started (since)")
        try:
            since = datetime.fromisoformat(str(since_raw)).timestamp() \
                if isinstance(since_raw, str) else float(since_raw)
            until = (datetime.fromisoformat(str(payload["until"])).timestamp()
                     if payload.get("until") else None)
        except ValueError:
            raise HTTPException(400, "since/until want a local time like 2026-10-27 14:00")
        folder = payload.get("folder")
        if folder:
            out = Path(folder).expanduser()
            home = Path.home()
            if home not in out.resolve().parents and out.resolve() != home:
                raise HTTPException(400, "the export folder must be under your home folder")
        else:
            out = house.data_dir / "takes" / datetime.now().strftime("%Y%m%d-%H%M")
        return export_media(house.data_dir, since, until, payload.get("robot"), out)

    # -- builder / workshops (spec 09) ---------------------------------------------
    @app.get("/api/families")
    async def families():
        return family_catalog()

    @app.get("/api/workshops")
    async def workshops_list():
        names = set(house.workshops)
        names |= {r["id"] for r in house.registry.all(kind="workshop")}
        names |= {r.get("workshop") for r in house.registry.all()
                  if r.get("workshop")}
        out = []
        for name in sorted(n for n in names if n):
            record = house.registry.get(name) or {}
            link = house.workshops.get(name)
            out.append({"name": name,
                        "display_name": record.get("display_name") or name,
                        "state": "online" if link else "offline",
                        "since": link.since if link else None})
        return out

    @app.get("/api/parts")
    async def parts():
        return []

    @app.get("/api/grounding")
    async def grounding():
        from . import grounding as grounding_mod

        health = await grounding_mod.health()
        return {"ready": health.get("state") == "ready",
                "state": health.get("state"),
                "model": health.get("model") or os.environ.get(
                    "HOUSE_SAM_MODEL", "sam-3.1"),
                "detail": health.get("error") or ""}

    def _get_build(bid: str):
        try:
            return builder.get(bid)
        except KeyError:
            raise HTTPException(404, f"no build '{bid}'")

    def _free_build(bid: str):
        build = _get_build(bid)
        if build.busy:
            raise HTTPException(409, "the builder is still busy with the last step")
        return build

    @app.get("/api/builds")
    async def builds_list():
        return [{"id": b.id, "created": b.created,
                 "name": b.data.get("name") or b.robot,
                 "family": b.family_ref or None,
                 "stage": builder.stage(b), "robot": b.robot}
                for b in builder.list_builds()]  # oldest first

    @app.post("/api/builds")
    async def builds_create(payload: dict = {}):
        build = builder.create()
        build.busy = True
        builder.save(build)
        house.spawn(builder.turn(
            build,
            "the person opened Add a robot: look for devices, then greet "
            "them and ask what they'd like to make", role="event"))
        return builder.sheet(build)

    @app.get("/api/builds/{bid}")
    async def build_one(bid: str):
        return builder.sheet(_get_build(bid))

    @app.delete("/api/builds/{bid}")
    async def build_delete(bid: str):
        build = _get_build(bid)
        await builder.discard(build)
        return {"ok": True}

    @app.get("/api/builds/{bid}/devices")
    async def build_devices(bid: str, workshop: str = "", fresh: bool = False):
        build = _get_build(bid)
        targets = [workshop] if workshop else sorted(house.workshops)
        devices: list = []
        try:
            for target in targets:
                devices.extend(await builder.devices_on(target, fresh=fresh))
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")
        return {"workshop": workshop, "devices": devices}

    @app.post("/api/builds/{bid}/device")
    async def build_device(bid: str, payload: dict):
        build = _free_build(bid)
        workshop = str(payload.get("workshop") or build.workshop or "")
        device = str(payload.get("device") or "")
        try:
            return await builder.press(
                build, f"Chose {device} on {workshop}",
                lambda: builder.choose_device(build, workshop, device))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    @app.post("/api/builds/{bid}/connect")
    async def build_connect(bid: str, payload: dict):
        build = _free_build(bid)
        fields = payload.get("fields")
        if not isinstance(fields, dict):
            fields = {k: v for k, v in (payload or {}).items()
                      if k != "fields"}
        try:
            return await builder.press(
                build, "Pressed connect",
                lambda: builder.connect(build, fields))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    @app.get("/api/builds/{bid}/draft")
    async def build_draft(bid: str):
        build = _get_build(bid)
        if not builder._is_draft(build):
            raise HTTPException(409, "not a draft")
        if not build.workshop:
            raise HTTPException(502, "builder error: no workshop for this draft")
        result = await house.act(f"@{build.workshop}", "draft_review",
                                 {"draft": build.id}, timeout=60)
        if not result.get("ok"):
            raise HTTPException(502, f"builder error: {result.get('text')}")
        data = result.get("data")
        return data if isinstance(data, dict) else {"text": result.get("text")}

    @app.post("/api/builds/{bid}/approve")
    async def build_approve(bid: str, payload: dict):
        build = _free_build(bid)
        try:
            await builder.approve_draft(build, str(payload.get("hash") or ""))
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")
        return builder.sheet(build)

    @app.post("/api/builds/{bid}/check/{cid}")
    async def build_check(bid: str, cid: str, payload: dict = {}):
        build = _free_build(bid)
        try:
            return await builder.press(
                build, f"Pressed check {cid}",
                lambda: builder.check(build, cid))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    @app.post("/api/builds/{bid}/mark/{cid}")
    async def build_mark(bid: str, cid: str, payload: dict):
        build = _free_build(bid)
        worked = bool(payload.get("worked"))
        entry = next((c for c in build.checks if str(c.get("id")) == cid), {})
        title = entry.get("title") or cid
        what = f"Said {title} worked" if worked else f"Said {title} didn't work"
        try:
            return await builder.press(
                build, what,
                lambda: builder.mark(build, cid, worked, payload.get("note")))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    @app.get("/api/builds/{bid}/check/{cid}.jpg")
    async def build_check_photo(bid: str, cid: str):
        build = _get_build(bid)
        path = house.data_dir / "builds" / build.id / f"{cid}.jpg"
        if not path.exists():
            raise HTTPException(404, "no check photo yet")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/builds/{bid}/soul")
    async def build_soul(bid: str, payload: dict = {}):
        build = _free_build(bid)
        try:
            return await builder.press(
                build, "Pressed give it a soul",
                lambda: builder.give_soul(build))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    @app.post("/api/builds/{bid}/say")
    async def build_say(bid: str, payload: dict):
        build = _free_build(bid)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "say something first — the message was empty")
        try:
            return await builder.turn(build, text, role="human")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"builder error: {exc}")

    # -- websockets -----------------------------------------------------------------------
    @app.websocket("/ws/body")
    async def ws_body(ws: WebSocket):
        await ws.accept()
        key: str | None = None
        tasks: set[asyncio.Task] = set()
        try:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
                hello = json.loads(raw)
            except Exception:
                await ws.close(code=4400)
                return
            if not isinstance(hello, dict):
                await ws.close(code=4400)
                return
            if house_token and hello.get("token") != house_token:
                await ws.close(code=4401)
                return
            manifest = hello.get("manifest") or {}
            if hello.get("type") != "hello" or not NAME_RE.match(str(manifest.get("name") or "")):
                await ws.close(code=4400)
                return
            key = await house.attach(ws, manifest)
            rid = key[1:] if key.startswith("@") else key
            await ws.send_json({"type": "welcome", "name": rid})
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    break
                if not isinstance(msg, dict):
                    continue
                task = asyncio.ensure_future(house.on_body_message(key, msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if key is not None:
                house.detach(key, ws)

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket):
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue()
        house.listeners.add(queue)
        try:
            while True:
                event = await queue.get()
                await ws.send_json(event)
        except Exception:
            pass
        finally:
            house.listeners.discard(queue)

    return app


app = create_app()

