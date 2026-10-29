"""Give this machine's workshop a networked robot's account, so it can stand in.

    python tools/stand_in.py <robot>

For when the workshop that holds the account is gone (while both are here the
house hands it over by itself). Runs the family's own connect step with the
account typed here, never echoed: it logs in to prove the account works and
keeps it in this workshop's secrets. The house moves the robot within two
minutes after that.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bodies"))

import families  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    rid = sys.argv[1]
    data = REPO / "data"
    config = None
    try:
        config = json.loads((data / "handover" / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = json.loads((data / "registry.json").read_text(encoding="utf-8"))["robots"].get(rid)
        if record:
            config = {"family": record.get("family"), "device": record.get("device") or {}}
    if not config or not config.get("family"):
        print(f"the house doesn't know a robot called {rid!r}")
        return 1
    fam = families.get(config["family"])
    if getattr(fam, "REACH", None) != "network":
        print(f"{rid} is on a cable to its own machine: it can only run there")
        return 1
    device = config.get("device") or {}
    fields = {}
    for field in fam.CONNECT.get("fields") or []:
        label = field["label"]
        if field.get("secret"):
            fields[field["id"]] = getpass.getpass(f"{label}: ")
        else:
            known = device.get("host") if field["id"] == "address" else ""
            typed = input(f"{label}{f' [{known}]' if known else ''}: ").strip()
            fields[field["id"]] = typed or known or ""
    text, _device = fam.connect(device, fields, config_path=str(data / "robots" / f"{rid}.json"))
    print(f"{text}. The account is kept on this workshop; the house moves {rid} here shortly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
