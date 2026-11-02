# Setting up Ponki

How to run each part of the house on your own machines: the hub, the voice and
ears, the camera and its finding service, the pins firmware, and the systemd
services. The [README](../README.md) has the overview.

## With Docker

`docker compose up --build` runs the hub, the finding service and a workshop
with the virtual desk, and starts the house with Pip in it (see the README).
What compose sets up, and where:

| Service | What it is | Settings that matter |
|---|---|---|
| `hub` | the house, on http://localhost:8200, and the door for robots on :8210 (both this machine only) | `META_API_KEY`, `HOUSE_HUMAN`, `HOUSE_TOKEN` from `.env` |
| `sam` | finding (SAM 3.1), reached by the hub as `http://sam:8201` | `META_API_KEY` |
| `workshop` | a workshop named `desk-workshop` with only the virtual desk | `HOUSE_DESK=on`, `HOUSE_WORKSHOP_FAMILIES=desk` |
| `voice` | `--profile voice`: Meta MMS-TTS, six voices made from its one speaker | none |

The house's memory lives in two volumes, `ponki_hub` and `ponki_workshop`. On the
first start the entrypoint copies `demo/hub` and `demo/workshop` into them once.
Ears are off: a container has no house mic, so you type to the robots.

The hub, the finding service and the voice have health checks, and the
workshop waits for a healthy hub. Logs rotate at 10 MB, three files per
service. Everything you might change goes in `.env` as a `PONKI_` setting
(see `.env.example`), so `compose.yaml` stays as it is and `git pull` stays
clean.

### Workshops on other machines

The house can run in Docker while workshops run next to the hardware: the Pi
with the Arduino on USB, a laptop near the rover. A workshop needs to be on
the machine its devices are plugged into, and USB and network discovery don't
reach into a container, so those workshops run natively.

The hub has two ports for this:

| Port | Answers | Default |
|---|---|---|
| `8200` | the web UI, its API and bodies | this machine only (`PONKI_BIND`) |
| `8210` | **only** bodies: `/ws/body` and voice clips for speakers, both token-checked | this machine only (`PONKI_BODY_BIND`) |

On the machine running the house, open the door for robots and set a real
token in `.env`:

```bash
PONKI_BODY_BIND=0.0.0.0
HOUSE_TOKEN=<python3 -c 'import secrets; print(secrets.token_urlsafe(32))'>
```

then `docker compose up -d`. If `PONKI_BODY_BIND` reaches beyond this machine
and the token is empty, the example value or shorter than 24 characters, the
hub doesn't start and says why.

On each other machine, `./setup.sh --workshop` asks for the door for robots
(`ws://<house machine>:8210`) and the token, then
`.venv/bin/python tools/start.py --workshop` starts it. By hand, it's:

```bash
HOUSE_HUB=ws://<house machine>:8210 HOUSE_TOKEN=<same token> HOUSE_WORKSHOP_NAME=pi-workshop \
  .venv/bin/python bodies/workshop/workshop.py
```

It shows up under System → Workshops, and its devices in Add a robot. To keep
it running, see [Run it as a service](#run-it-as-a-service).

- **The web UI stays on this machine.** `PONKI_BIND=0.0.0.0` publishes it too,
  but it has no login: only on a LAN you trust or a tailnet.
- **Across a network you don't trust**, put `8210` behind a TLS proxy (Caddy,
  Tailscale Serve) and use `wss://` in `HOUSE_HUB`; the token travels in the
  first message.
- **Sidecars on the house machine itself** (ears with a real mic, for one) are
  reachable from the hub as `host.docker.internal`, on Linux too:
  `PONKI_EARS_URL=http://host.docker.internal:8203`.

## Without Docker: setup.sh and start.py

`./setup.sh` (macOS or Linux, Python 3.10 to 3.13) makes `.venv` and, with
Node.js 20.17+, `services/sam/.venv`; creates `.env` from `.env.example`
unless there is one (mode 600); asks for `META_API_KEY` (not echoed) and
`HOUSE_HUMAN`; and, while `HOUSE_TOKEN` is empty or an example value, puts
in `secrets.token_urlsafe(32)`: 43 characters of `A-Z a-z 0-9 - _`, safe in
`.env`, systemd, compose and a URL, and well over the 24 the door for robots
asks for. A token you already have is kept, since every workshop uses it.
On a first run it copies the demo house (Pip) into `data/`. `--workshop`
asks for `HOUSE_HUB` and the house's token instead; `--yes` asks nothing.

`.venv/bin/python tools/start.py` runs the hub (this machine only, like
Docker), a workshop named `desk-workshop` with the virtual desk that also
looks for real devices, and each service with a `.venv` of its own: finding,
and the voice and the ears once you've made theirs (below). `--lan` opens
the UI and the door for robots (:8210) to your network, and the hub won't
start with a weak token. `--workshop` runs only a workshop. Settings come
from `.env`, and the environment wins. For a house that starts with the
machine, use the systemd units ([Run it as a service](#run-it-as-a-service)).

## Run the hub (Step 1)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # set HOUSE_TOKEN at least
.venv/bin/uvicorn hub.main:app --host 0.0.0.0 --port 8200
```

Then in another shell:

```bash
HOUSE_HUB=ws://localhost:8200 HOUSE_TOKEN=<same token> .venv/bin/python bodies/sdk/example.py
```

Open http://localhost:8200/ — the tester robot appears on the board and its
actions run from the Body tab.

## The house voice and hearing

Every model Ponki calls at runtime is a Meta model. Robots without a
speaker speak through the house voice: Meta's MMS-TTS
(`facebook/mms-tts-eng`, CC-BY-NC 4.0), run locally on the CPU at 48 kHz.
It has one English speaker, so the six voices (warm, bright, small, low,
playful, soft) are presets of its pitch and pace. The house hears people
through a mic fixture and the ears (Muse Voice Transcribe, which needs
`META_API_KEY`; there is no offline fallback).

A robot with a speaker of its own can speak in the house voice too: its body
declares `voice_play`, the name of an action that takes a clip's `url`
(`/api/voice/<n>.wav`), fetches it with `body.house_clip(url)` and plays it.
The house then sends that robot's lines to its own speaker instead of the
house speakers. RoBoHoN's `voice` setting (in his config, or
`HOUSE_ROBOHON_VOICE`) picks one of three:

| `voice` | He speaks with | Notes |
|---|---|---|
| `house` (default) | the house voice, on the house speakers | every model stays a Meta model |
| `stream` | the house voice, on his own speaker | needs his server to offer `POST /api/play_wav` |
| `own` | his server's own voices | Kokoro, Piper, StyleTTS2: not Meta models |

```bash
python3 -m venv services/voice/.venv
services/voice/.venv/bin/pip install -r services/voice/requirements.txt
# MMS-TTS (facebook/mms-tts-eng) downloads from Hugging Face on the first start
python3 -m venv services/ears/.venv
services/ears/.venv/bin/pip install -r services/ears/requirements.txt
./deploy/install.sh ponkihouse-voice ponkihouse-ears
sudo systemctl enable --now ponkihouse-voice ponkihouse-ears
```

On the Pi, the speaker and mic run as fixtures
(`ponkihouse-speaker`, `ponkihouse-bt-speaker`, `ponkihouse-mic`). The
speaker feeds a `pcm.house` in `~/.asoundrc` (not in git): a `plug` to
bluealsa at **48 kHz stereo** — a mono stream makes bluealsa renegotiate a
distorted low-bitrate mono link. The Bluetooth radio may need
`rfkill unblock bluetooth` once. `bt_keepalive.sh` keeps the JBL bonded
(a pairing agent must be registered when pairing, or no bond is stored),
connected and at volume on both channels.

## The camera and the finding service

A Tapo-style ONVIF camera becomes a robot that looks, turns, and finds
things. It needs a **Camera Account** (Tapo app → the camera → ⚙️ → Advanced
Settings → Camera Account; on some firmware also Me → Tapo Lab → Third-Party
Compatibility, and Privacy Mode off). The account lives only in the
workshops' `data/secrets/`, never stored by the hub. The driver
(`bodies/onvif/onvif_body.py <robot.json>`) runs on the Pi workshop; its
Python deps are pinned in `bodies/onvif/requirements.txt`.

**A stand-in for the camera.** The NUC's workshop also enables the camera
family (`HOUSE_WORKSHOP_FAMILIES=robohon,onvif` in
`ponkihouse-workshop-nuc`), with the same pins in the hub's venv:

```bash
.venv/bin/pip install -r bodies/onvif/requirements.txt
```

When the Pi leaves, the hub moves the camera to the NUC after a minute
(`HOUSE_TAKEOVER_AFTER`, seconds; `HOUSE_TAKEOVER=off` turns it off). While
both are home the Pi hands the NUC the Camera Account by itself. If the Pi
went before it could, give it once on the NUC (typed, never echoed; it logs
in to prove it works):

```bash
.venv/bin/python tools/stand_in.py keiki
```

When the Pi comes back, it sets its old copy aside and the camera stays on
the NUC.

Finding things (`locate` / `watch`) is done by SAM 3.1 on Meta's Model API,
through a small local service on the NUC:

```bash
python3 -m venv services/sam/.venv
services/sam/.venv/bin/pip install -r services/sam/requirements.txt
(cd services/sam/js && npm install)   # the mask decoder worker
./deploy/install.sh ponkihouse-sam
sudo systemctl enable --now ponkihouse-sam   # 127.0.0.1:8201
```

## Flashing an Arduino (the pins firmware)

One generic sketch turns any Uno/Nano into a board the house configures at
runtime; a robot is then a config file of parts, never a new sketch.
Installed and pinned on the Pi workshop: **arduino:avr 1.8.8**,
**Servo 1.3.0**, `arduino-cli` from the official Arduino release
(checksum verified) at `~/bin/arduino-cli` (found via `$ARDUINO_CLI`,
else `~/bin/arduino-cli`, else the PATH).

```bash
arduino-cli core update-index
arduino-cli core install arduino:avr   # 1.8.8
arduino-cli lib install Servo          # 1.3.0
arduino-cli compile -b arduino:avr:uno -u -p <port> firmware/pins
```

Tests (standard library only, no hardware, no model calls):

```bash
.venv/bin/python -m unittest discover -s tests
```

## Run it as a service

Clone anywhere, then let the installer fill in where the repo lives — the
unit files themselves carry no machine paths. Every unit on every machine
is prefixed `ponkihouse-`: the hub is `ponkihouse-hub` (later
`ponkihouse-voice`, `ponkihouse-ears`, `ponkihouse-sam`, and
`ponkihouse-workshop` — the Pi's and the NUC's workshop are the same unit
with a different `.env`).

```bash
git clone <this repo> ~/ponkihouse   # any folder works
cd ~/ponkihouse
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                # set HOUSE_TOKEN at least
./deploy/install.sh                 # fills in this clone's path and your user
sudo systemctl enable --now ponkihouse-hub
sudo tailscale serve --bg --https=8445 http://127.0.0.1:8200
```

Then open `https://<this-machine>.<your-tailnet>.ts.net:8445/` from any
device on your tailnet.
