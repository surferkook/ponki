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

## Run it as a service (this NUC)

All house services are prefixed `ponki-`: the hub is `ponki-house`
(later: `ponki-voice`, `ponki-ears`, `ponki-sam`, `ponki-workshop`).

```bash
sudo cp deploy/ponki-house.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ponki-house
sudo tailscale serve --bg --https=8445 http://127.0.0.1:8200
```

Then open `https://<this-machine>.<your-tailnet>.ts.net:8445/` from any
device on your tailnet.
