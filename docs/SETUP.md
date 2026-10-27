# Setting up Ponki

How to run each part of the house on your own machines: the hub, the voice and
ears, the camera and its finding service, the pins firmware, and the systemd
services. The [README](../README.md) has the overview.

> No UI login yet: the hub is LAN + tailnet only (ufw 192.168.1.0/24). Do not
> expose port 8200 to the internet.

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

Robots without a speaker speak through the house voice (Kokoro + Piper,
all 48 kHz), and the house hears people through a mic fixture and the ears
(Muse Voice Transcribe, with faster-whisper offline as fallback).

```bash
python3 -m venv services/voice/.venv
services/voice/.venv/bin/pip install -r services/voice/requirements.txt
# Piper models (not in git), from rhasspy/piper-voices on Hugging Face:
#   en/en_US/{amy,hfc_female,ryan}/medium/*.onnx(+ .json) → services/voice/voices/
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
workshop's `data/secrets/`, never in the hub. The driver
(`bodies/onvif/onvif_body.py <robot.json>`) runs on the Pi workshop; its
Python deps are pinned in `bodies/onvif/requirements.txt`.

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
