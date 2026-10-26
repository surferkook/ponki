"""Copy one session's media into a folder (spec 02).

    python -m hub.export --last 2h --out ~/takes/take1
    python -m hub.export --since "2026-10-27 14:00" [--until ...] [--robot keiki]

Read-only on data/: walks the feed for events in the window and copies every
referenced file that still exists. Deleted files are listed as missing.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

MEDIA_KINDS = ("saw", "located", "watched", "watch_media", "voice")


def format_event_line(event: dict) -> str:
    stamp = datetime.fromtimestamp(float(event.get("t", 0))).isoformat(timespec="seconds")
    rest = {k: v for k, v in event.items() if k not in ("t", "kind", "robot", "found")}
    return (f"{stamp} {str(event.get('kind', '')):14} {str(event.get('robot') or '-'):10} "
            f"{json.dumps(rest, ensure_ascii=False)[:400]}")


def _referenced(data_dir: Path, event: dict) -> list[tuple[Path, str]]:
    """(source path, copied file name) for every media file an event names."""
    out: list[tuple[Path, str]] = []
    stamp = datetime.fromtimestamp(float(event.get("t", 0))).strftime("%H%M%S")
    rid = event.get("robot") or ""
    for field_name in ("image", "gif", "video", "url"):
        url = event.get(field_name)
        if not isinstance(url, str):
            continue
        parts = url.strip("/").split("/")
        # /api/robots/<id>/seen/<n>.<ext>
        if len(parts) == 5 and parts[:2] == ["api", "robots"] and parts[3] == "seen":
            owner, filename = parts[2], parts[4]
            out.append((data_dir / "seen" / owner / filename,
                        f"{stamp}-{owner}-{filename}"))
        # /api/voice/<n>.wav
        elif len(parts) == 3 and parts[:2] == ["api", "voice"]:
            out.append((data_dir / "voice" / parts[2],
                        f"{stamp}-{rid}-voice-{parts[2]}"))
    return out


def export_media(data_dir: Path, since: float, until: float | None,
                 robot: str | None, out_dir: Path) -> dict:
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    events: list[dict] = []
    feed_path = data_dir / "events.jsonl"
    if feed_path.exists():
        for line in feed_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            when = float(event.get("t", 0))
            if when < since or (until is not None and when > until):
                continue
            if robot and event.get("robot") != robot:
                continue
            events.append(event)
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    missing: list[str] = []
    index: list[dict] = []
    for event in events:
        entry = dict(event)
        entry["files"] = []
        if event.get("kind") in MEDIA_KINDS:
            for source, name in _referenced(data_dir, event):
                if source.exists():
                    shutil.copyfile(source, out_dir / name)
                    entry["files"].append(name)
                    copied += 1
                else:
                    missing.append(name)
        index.append(entry)
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (out_dir / "feed.txt").write_text(
        "".join(format_event_line(e) + "\n" for e in events), encoding="utf-8")
    return {"folder": str(out_dir), "copied": copied, "missing": missing}


def _parse_last(text: str) -> timedelta:
    units = {"h": "hours", "m": "minutes", "s": "seconds"}
    if not text or text[-1] not in units or not text[:-1].isdigit():
        raise SystemExit(f"--last wants something like 2h, 30m or 90s, not {text!r}")
    return timedelta(**{units[text[-1]]: int(text[:-1])})


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Copy one session's media into a folder.")
    parser.add_argument("--since", help='local time, e.g. "2026-10-27 14:00"')
    parser.add_argument("--until", help="local time, same shape as --since")
    parser.add_argument("--last", help="instead of --since: 2h, 30m, 90s")
    parser.add_argument("--robot")
    parser.add_argument("--out", required=True)
    parser.add_argument("--data", default=str(Path(__file__).parent.parent / "data"))
    args = parser.parse_args(argv)
    now = datetime.now()
    if args.last:
        since = (now - _parse_last(args.last)).timestamp()
    elif args.since:
        since = datetime.fromisoformat(args.since).timestamp()
    else:
        parser.error("give --since or --last")
    until = datetime.fromisoformat(args.until).timestamp() if args.until else None
    result = export_media(Path(args.data), since, until, args.robot,
                          Path(args.out).expanduser())
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
