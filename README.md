# Ponki — a home for robot souls

Cheap and odd devices join one house and each becomes a robot with its own soul:
a persona, memories, a voice and a working conversation with a model. The soul
lives in the house; the body is just where it acts.

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
