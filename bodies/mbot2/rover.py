# house-rover: the program that runs ON the mBot2 (spec 07).
# A template: the workshop renders the five settings in, uploads it over
# USB, and from then on the rover dials its workshop over Wi-Fi and runs a
# whitelist of named commands with clamped values. Never code from the wire.
# (This file must stay valid CPython syntax too: the workshop compiles the
# rendered program before uploading, so a typo never reaches the board.)
import _thread
import time

import cyberpi
import mbot2
import mbuild
import ujson
import usocket

SSID = "{ssid}"
PASSWORD = "{password}"
HOST = "{host}"
PORT = {port}
KEY = "{key}"
VERSION = 2
QUIET = 10
MAX_CM = 100
MAX_DEG = 360
MAX_SPEED = 100

FACES = {"happy": "^_^", "sad": "T_T", "surprised": "O_O", "sleepy": "-_-",
         "love": "<3", "wink": "^_~", "angry": ">_<", "neutral": "o_o"}
COLORS = {"red": (255, 0, 0), "green": (0, 200, 0), "blue": (0, 60, 255),
          "yellow": (255, 180, 0), "purple": (160, 0, 255),
          "white": (200, 200, 200), "orange": (255, 90, 0), "off": (0, 0, 0)}
EYES = ("happy", "wink", "thinking", "dizzy", "standby")

NAME = "Rover"


def status(text, draw=True):
    print("HOUSE " + text)
    if draw:
        try:
            cyberpi.display.clear()
            cyberpi.display.show_label(NAME + "\n" + text, 16, "center")
        except Exception:
            pass


def why(exc):
    err = exc.args[0] if exc.args else None
    if err in (104, 111):
        return "the workshop isn't listening"
    if err == 113:
        return "can't reach the workshop"
    if err == 118:
        return "can't reach the workshop (no address yet?)"
    if err in (110, 116):
        return "the workshop didn't answer"
    return str(exc)


def clamp(value, low, high, fallback):
    try:
        value = float(value)
    except Exception:
        return fallback
    return max(low, min(high, value))


def safe(fn, fallback=None):
    try:
        return fn()
    except Exception:
        return fallback


# -- the move thread ---------------------------------------------------------

_move = {"kind": None, "amount": 0, "error": None, "done": True}


def _run_move(kind, call):
    try:
        call()
    except Exception as exc:
        _move["error"] = str(exc)
    _move["kind"] = None
    _move["done"] = True


def start_move(kind, call, amount):
    _move["kind"] = kind
    _move["amount"] = amount
    _move["error"] = None
    _move["done"] = False
    _thread.start_new_thread(_run_move, (kind, call))


# -- the command whitelist -----------------------------------------------------

def command(msg):
    """-> (reply dict or None, pending id or None)"""
    global NAME
    do = msg.get("do")
    mid = msg.get("id")

    def ok(ret=None):
        return {"id": mid, "ok": True, "ret": ret}, None

    def fail(text):
        return {"id": mid, "ok": False, "error": text}, None

    if do == "ping":
        return ok({"battery": safe(cyberpi.get_battery)})
    if do == "stop":
        safe(lambda: mbot2.EM_stop("all"))
        return ok("stopped")
    if do in ("drive", "turn"):
        if _move["kind"]:
            return fail("still moving (" + _move["kind"] + ")")
        if do == "drive":
            cm = clamp(msg.get("cm", 0), -MAX_CM, MAX_CM, 0)
            speed = clamp(msg.get("speed", 50), 5, MAX_SPEED, 50)
            start_move("drive", lambda: mbot2.straight(cm, speed), cm)
            return None, mid
        degrees = clamp(msg.get("degrees", 0), -MAX_DEG, MAX_DEG, 0)
        speed = clamp(msg.get("speed", 50), 5, MAX_SPEED, 50)
        start_move("turn", lambda: mbot2.turn(degrees, speed), degrees)
        return None, mid
    if do == "show":
        text = str(msg.get("text", ""))[:60]
        size = int(clamp(msg.get("size", 16), 12, 32, 16))
        safe(lambda: cyberpi.display.clear())
        safe(lambda: cyberpi.display.show_label(text, size, "center"))
        return ok()
    if do == "face":
        face = FACES.get(msg.get("mood"), "o_o")
        safe(lambda: cyberpi.display.clear())
        safe(lambda: cyberpi.display.show_label(face, 32, "center"))
        return ok()
    if do == "light":
        rgb = COLORS.get(msg.get("color"), (0, 0, 0))
        safe(lambda: cyberpi.led.on(rgb[0], rgb[1], rgb[2]))
        return ok()
    if do == "eyes":
        if msg.get("expression") is not None:
            expr = msg.get("expression")
            if expr not in EYES:
                return fail("no eye expression " + str(expr))
            safe(lambda: mbuild.ultrasonic2.play(expr, 1))
            return ok()
        level = int(clamp(msg.get("level", 100), 0, 100, 100))
        safe(lambda: mbuild.ultrasonic2.set_bri(level, "all", 1))
        return ok()
    if do == "beep":
        hz = clamp(msg.get("hz", 880), 100, 4000, 880)
        seconds = clamp(msg.get("s", 0.2), 0.05, 2, 0.2)
        safe(lambda: cyberpi.audio.play_tone(hz, seconds))
        return ok()
    if do == "sense":
        distance = safe(lambda: mbuild.ultrasonic2.get(1))
        if distance is not None and distance >= 300:
            distance = None
        return ok({"battery": safe(cyberpi.get_battery),
                   "heading": safe(cyberpi.get_yaw),
                   "distance_cm": distance,
                   "pitch": safe(cyberpi.get_pitch),
                   "roll": safe(cyberpi.get_roll)})
    if do == "reset_heading":
        safe(lambda: cyberpi.reset_yaw())
        return ok()
    if do == "name":
        NAME = str(msg.get("text", "Rover"))[:20] or "Rover"
        safe(lambda: cyberpi.display.clear())
        safe(lambda: cyberpi.display.show_label(NAME, 24, "center"))
        return ok()
    return fail("unknown command " + str(do))


def send(sock, obj):
    sock.send((ujson.dumps(obj) + "\n").encode())


# -- sessions -------------------------------------------------------------------

def session():
    sock = usocket.socket()
    sock.settimeout(5)
    info = usocket.getaddrinfo(HOST, PORT)[0][-1]
    sock.connect(info)
    sock.settimeout(0.2)
    mac = safe(cyberpi.get_mac_address, "")
    send(sock, {"hello": "house-rover", "v": VERSION, "key": KEY,
                "mac": mac, "battery": safe(cyberpi.get_battery)})
    status("with the house")
    last_heard = time.time()
    pending = None
    buf = b""
    try:
        while True:
            if pending is not None and _move["done"]:
                if _move["error"]:
                    send(sock, {"id": pending, "ok": False,
                                "error": _move["error"]})
                else:
                    send(sock, {"id": pending, "ok": True,
                                "ret": {"done": pending_kind[0],
                                        "amount": _move["amount"]}})
                pending = None
            try:
                data = sock.recv(512)
            except Exception as exc:
                if "timed out" in str(exc) or (exc.args and exc.args[0] == 110):
                    data = None
                else:
                    raise
            if data == b"":
                status("the workshop hung up", draw=False)
                return
            if data:
                last_heard = time.time()
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = ujson.loads(line)
                    except Exception:
                        continue
                    reply, pend = command(msg)
                    if pend is not None:
                        pending = pend
                        pending_kind[0] = msg.get("do")
                    elif reply is not None and msg.get("id") is not None:
                        send(sock, reply)
            elif time.time() - last_heard > QUIET:
                status("the workshop went quiet")
                return
    finally:
        safe(lambda: mbot2.EM_stop("all"))
        try:
            sock.close()
        except Exception:
            pass


pending_kind = ["drive"]


def join_wifi():
    if safe(cyberpi.wifi.is_connect, False):
        return True
    status("Wi-Fi: joining " + SSID)
    safe(lambda: cyberpi.wifi.connect(SSID, PASSWORD))
    for _ in range(120):  # join once, then just wait (asking again restarts it)
        if safe(cyberpi.wifi.is_connect, False):
            status("Wi-Fi: joined")
            time.sleep(2)  # the address isn't there straight away
            return True
        time.sleep(0.5)
    status("Wi-Fi: can't join " + SSID)
    return False


def main():
    safe(lambda: cyberpi.led.on(0, 0, 0))
    status("starting")
    while True:
        try:
            if join_wifi():
                status("calling the house")
                session()
        except Exception as exc:
            status("lost the house: " + why(exc))
        time.sleep(3)


main()
