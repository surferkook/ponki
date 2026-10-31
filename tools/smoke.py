"""Smoke test: is a freshly started house what a newcomer should find?

    python tools/smoke.py [--hub http://localhost:8200] [--voice] [--e2e]

Run it against a house started from scratch (docker compose up, empty
volumes). Standard library only. Without a key it checks everything that
needs no model: the services, the workshop and its virtual desk, Pip seeded
and online, a photo from Pip's camera, the lamp waiting in Add a robot, the
web page. --voice also checks the house voice speaks. --e2e (needs
META_API_KEY in the house) asks Pip to find the orange: Muse Spark plus
SAM 3.1, end to end.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

FAILED: list[str] = []


def call(hub: str, path: str, body: dict | None = None, method: str | None = None,
         timeout: float = 30) -> tuple[int, dict | bytes, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(hub + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, kind = resp.read(), resp.headers.get("Content-Type", "")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, kind, status = exc.read(), exc.headers.get("Content-Type", ""), exc.code
    if "json" in kind:
        return status, json.loads(raw or b"null"), kind
    return status, raw, kind


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {what}{f'  ({detail})' if detail and not ok else ''}", flush=True)
    if not ok:
        FAILED.append(what)
    return ok


def wait(what: str, test, seconds: float, every: float = 2.0):
    """Poll test() until it returns something truthy; report how long it took."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < seconds:
        try:
            last = test()
            if last:
                print(f"ok   {what} ({time.monotonic() - started:.0f} s)", flush=True)
                return last
        except Exception as exc:  # the hub may still be starting
            last = exc
        time.sleep(every)
    check(False, what, f"not within {seconds:.0f} s; last: {last!r}"[:300])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hub", default="http://localhost:8200")
    ap.add_argument("--voice", action="store_true", help="the voice profile is running")
    ap.add_argument("--e2e", action="store_true", help="a real Pip turn (needs META_API_KEY)")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for start-up")
    args = ap.parse_args()
    hub = args.hub.rstrip("/")

    system = wait("the hub answers", lambda: call(hub, "/api/system", timeout=5)[1], args.wait)
    if not system:
        return 1
    status, page, _ = call(hub, "/")
    check(status == 200 and b"Ponki" in page, "the web page loads")
    info = call(hub, "/api/info")[1]
    print(f"     brain {info['model']}, API key {'set' if info.get('key') else 'not set'}")
    if args.e2e:
        check(bool(info.get("key")), "the house has a Meta API key")

    services = system["services"]
    check(services["ears"]["state"] == "off", "ears are off (a container has no house mic)",
          services["ears"]["state"])
    if info.get("key"):
        check(services["finding"]["ready"], "finding (SAM 3.1) is ready", services["finding"]["state"])
    else:
        check(services["finding"]["state"] == "error"
              and "META_API_KEY" in (services["finding"]["detail"] or ""),
              "finding says it needs the API key", json.dumps(services["finding"]))
    if args.voice:
        wait("the voice is ready", lambda: call(hub, "/api/system")[1]["services"]["voice"]["ready"],
             args.wait * 2, every=5)
    else:
        check(services["voice"]["state"] == "off", "the voice is off, not broken",
              services["voice"]["state"])

    def desk_workshop():
        for w in call(hub, "/api/system")[1]["workshops"]:
            if w["id"] == "desk-workshop" and w["online"] and "desk" in w["families"]:
                return w
    wait("the desk workshop is online with the virtual desk", desk_workshop, args.wait)

    def pip_online():
        robots = {r["name"]: r for r in call(hub, "/api/robots")[1]}
        pip = robots.get("pip")
        return pip if pip and pip["online"] and pip.get("persona") else None
    pip = wait("Pip is seeded, has a soul and is online", pip_online, args.wait)
    if pip:
        check((pip.get("record") or {}).get("family") == "desk", "Pip lives on the virtual desk")
        status, portrait, kind = call(hub, "/api/robots/pip/portrait.webp")
        check(status == 200 and kind.startswith("image/"), "Pip has a portrait", f"{status} {kind}")

        result = call(hub, "/api/robots/pip/act", {"action": "look", "args": {}}, timeout=60)[1]
        check(bool(result.get("ok")), "Pip's camera takes a photo", json.dumps(result)[:200])
        events = call(hub, "/api/events")[1]
        saw = [e for e in events if e.get("kind") == "saw" and e.get("robot") == "pip" and e.get("image")]
        if check(bool(saw), "the photo reaches the house feed"):
            status, jpeg, kind = call(hub, saw[-1]["image"])
            check(status == 200 and jpeg[:2] == b"\xff\xd8" and len(jpeg) > 20000,
                  "the photo is a real picture of the desk", f"{status} {kind} {len(jpeg)} bytes")
        result = call(hub, "/api/robots/pip/act", {"action": "turn", "args": {"pan": 40}}, timeout=60)[1]
        check(bool(result.get("ok")) and "right" in result.get("text", ""), "Pip turns its head",
              json.dumps(result)[:200])
        call(hub, "/api/robots/pip/act", {"action": "look_home", "args": {}}, timeout=60)

    # Add a robot: the lamp is waiting (the builder's greeting needs the key;
    # finding devices does not)
    build = call(hub, "/api/builds", {})[1]
    devices = call(hub, f"/api/builds/{build['id']}/devices", timeout=60)[1].get("devices", [])
    lamp = next((d for d in devices if d.get("id") == "desk:lamp"), None)
    camera = next((d for d in devices if d.get("id") == "desk:camera"), None)
    check(bool(lamp and lamp.get("ready")), "Add a robot finds the desk lamp, ready to build",
          json.dumps(devices)[:300])
    check(bool(camera and not camera.get("ready") and camera.get("robot") == "pip"),
          "and knows the desk camera is Pip's", json.dumps(camera)[:200])
    call(hub, f"/api/builds/{build['id']}", method="DELETE")

    if args.voice:
        status, wav, kind = call(hub, "/api/robots/pip/voice/preview", {"id": "am_puck"}, timeout=120)
        check(status == 200 and wav[:4] == b"RIFF" and len(wav) > 20000, "the house voice speaks (Kokoro)",
              f"{status} {kind} {wav[:120]!r}")
    else:
        status, body, _ = call(hub, "/api/robots/pip/voice/preview", {"id": "am_puck"})
        check(status == 503 and "--profile voice" in str(body), "without voice, it says how to turn it on",
              f"{status} {body}")

    if args.e2e:
        before = len(call(hub, "/api/events")[1])
        reply = call(hub, "/api/robots/pip/chat",
                     {"text": "Hi Pip! Where is the orange on the desk?"}, timeout=240)[1]
        check(bool((reply or {}).get("reply")), "Pip answers (Muse Spark)", json.dumps(reply)[:200])
        print(f"     Pip: {(reply or {}).get('reply', '')}")
        located = [e for e in call(hub, "/api/events")[1][before:] if e.get("kind") == "located"]
        oranges = [i for e in located for i in (e.get("found") or {}).get("orange", [])]
        check(any(i.get("score", 0) > 0.5 for i in oranges), "Pip finds the orange (SAM 3.1)",
              json.dumps(located)[:300])

    print(f"\n{'all good' if not FAILED else f'{len(FAILED)} failed: ' + '; '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
