#!/bin/sh
# Set Ponki up on this machine, without Docker (macOS or Linux).
#
#   ./setup.sh               the house: the hub, finding (SAM 3.1) and a workshop
#   ./setup.sh --workshop    only a workshop, for a machine with devices that
#                            joins a house running somewhere else
#   ./setup.sh --yes         ask nothing (fill in .env yourself afterwards)
#
# It makes .venv (and services/sam/.venv), creates .env from .env.example
# if there isn't one, asks for the few settings that matter (Enter skips
# any of them) and gives the house a long random HOUSE_TOKEN. Running it
# again is safe: it keeps your .env and your house, and only fills in what
# is still empty. It starts nothing and installs no system services; it
# ends by saying how to start Ponki.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

MODE=house
ASK=yes
for arg in "$@"; do
    case $arg in
        --workshop) MODE=workshop ;;
        --yes|-y) ASK=no ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done
[ -t 0 ] || ASK=no   # no terminal to answer on: ask nothing
# a hidden prompt turns echo off: always turn it back on
trap 'stty echo 2>/dev/null || true' EXIT
trap 'exit 130' INT TERM

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf '\nsetup stopped: %s\n' "$*" >&2; exit 1; }

# -- Python 3.10 to 3.13 -----------------------------------------------------------

step "Python"
PYTHON=
for candidate in ${PONKI_PYTHON:-} python3 python3.12 python3.11 python3.13 python3.10; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)' \
            >/dev/null 2>&1; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PYTHON" ]; then
    found=$(python3 --version 2>/dev/null || echo "no python3")
    case $(uname -s) in
        Darwin) hint="brew install python@3.12   (https://brew.sh), then run ./setup.sh again" ;;
        *) hint="install python3.12 and python3.12-venv with your package manager, then run ./setup.sh again" ;;
    esac
    die "Ponki needs Python 3.10 to 3.13 (found: $found).
  $hint
  Or point at one: PONKI_PYTHON=/path/to/python3.12 ./setup.sh"
fi
say "using $PYTHON ($("$PYTHON" --version 2>&1))"

make_venv() {  # folder, requirement files...
    venv=$1
    shift
    if [ ! -x "$venv/bin/python" ]; then
        "$PYTHON" -m venv "$venv" 2>/dev/null || {
            rm -rf "$venv"
            die "couldn't make a virtual environment with $PYTHON.
  On Debian or Ubuntu: sudo apt install python3-venv (or python3.X-venv for your version)"
        }
    fi
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    for req in "$@"; do
        "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$req" \
            || die "installing $req failed (see above)"
    done
}

step "Ponki's packages (.venv)"
make_venv .venv requirements.txt
say "ok"

SAM=no
if [ "$MODE" = house ]; then
    step "Finding: SAM 3.1 (services/sam/.venv)"
    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
       && node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit((a === 20 && b >= 17) || (a === 22 && b >= 9) || a > 22 ? 0 : 1)'; then
        make_venv services/sam/.venv services/sam/requirements.txt
        (cd services/sam/js && npm ci --omit=dev --no-audit --no-fund --loglevel=error) \
            || die "installing the mask decoder (npm) failed (see above)"
        SAM=yes
        say "ok"
    else
        say "skipped: it needs Node.js 20.17+ or 22.9+ (https://nodejs.org)."
        say "The house runs without it, but robots can't find things. Run ./setup.sh again once Node is in."
    fi
fi

# -- .env -------------------------------------------------------------------------

step "Settings (.env)"
if [ -e .env ]; then
    say "keeping your .env"
else
    cp .env.example .env
    say "made .env from .env.example"
fi
chmod 600 .env  # it holds your API key

# One edit at a time, by Python, so any character in a value is safe and the
# value is never on a command line. KEY=value lines only; the rest stays.
env_get() {
    KEY=$1 .venv/bin/python - <<'EOF'
import os
key = os.environ["KEY"]
for line in open(".env", encoding="utf-8"):
    if line.startswith(key + "="):
        print(line[len(key) + 1:].rstrip("\n"))
        break
EOF
}
env_set() {
    KEY=$1 VALUE=$2 .venv/bin/python - <<'EOF'
import os
key, value = os.environ["KEY"], os.environ["VALUE"]
lines = open(".env", encoding="utf-8").read().splitlines()
for i, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
tmp = ".env.tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as out:
    out.write("\n".join(lines) + "\n")
os.replace(tmp, ".env")
EOF
}
ask() {  # prompt, then the answer on stdout ('' when skipped)
    printf '%s' "$1" >&2
    IFS= read -r answer || answer=
    printf '%s' "$answer"
}
ask_secret() {
    printf '%s' "$1" >&2
    stty -echo 2>/dev/null || true
    IFS= read -r answer || answer=
    stty echo 2>/dev/null || true
    printf '\n' >&2
    printf '%s' "$answer"
}
# A value someone pastes: no spaces around it, and no line breaks or spaces inside.
clean() { printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }
TODO=

if [ "$MODE" = house ]; then
    if [ -z "$(env_get META_API_KEY)" ]; then
        key=
        [ "$ASK" = yes ] && key=$(clean "$(ask_secret "Your Meta Model API key (typed, not shown; Enter to skip): ")")
        case $key in
            *[[:space:]]*) say "that has a space in it; skipped"; key= ;;
        esac
        if [ -n "$key" ]; then env_set META_API_KEY "$key"; say "META_API_KEY saved"
        else TODO="$TODO
  META_API_KEY=   your Meta Model API key: souls, the builder and finding need it"; fi
    else
        say "META_API_KEY is already set"
    fi
    if [ -z "$(env_get HOUSE_HUMAN)" ]; then
        name=
        [ "$ASK" = yes ] && name=$(clean "$(ask "Your first name, so the robots know you (Enter to skip): ")")
        if [ -n "$name" ]; then env_set HOUSE_HUMAN "$name"; say "HOUSE_HUMAN saved"
        else TODO="$TODO
  HOUSE_HUMAN=    your first name (optional)"; fi
    else
        say "HOUSE_HUMAN is already set"
    fi
    # The house's own token: workshops and bodies present it. A fresh one
    # unless .env already has a real one (never replace it: every workshop
    # that joined uses it). 32 random bytes, URL-safe base64: 43 characters
    # of A-Z a-z 0-9 - _, safe in .env, systemd, compose and a URL.
    token=$(env_get HOUSE_TOKEN)
    case $token in
        ""|change-me-to-a-long-random-string|ponki-in-a-box)
            env_set HOUSE_TOKEN "$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
            say "HOUSE_TOKEN: a new random one (43 characters)" ;;
        *)
            if [ ${#token} -lt 24 ]; then
                say "HOUSE_TOKEN is shorter than 24 characters: fine on this machine, but a house"
                say "open to other machines won't start with it. Delete the line and run ./setup.sh to get a new one."
            else
                say "HOUSE_TOKEN: keeping yours"
            fi ;;
    esac
    case $(env_get HOUSE_HUB) in
        ""|ws://192.0.2.7:8200) env_set HOUSE_HUB "ws://localhost:8200" ;;
    esac
else
    hub=$(env_get HOUSE_HUB)
    case $hub in
        ""|ws://192.0.2.7:8200)
            hub=
            [ "$ASK" = yes ] && hub=$(clean "$(ask "Where is the house? e.g. 192.0.2.10 (its door for robots, :8210), or a ws:// address (Enter to skip): ")")
            case $hub in
                "") ;;
                ws://*|wss://*) ;;
                *:*) hub="ws://$hub" ;;
                *) hub="ws://$hub:8210" ;;
            esac
            if [ -n "$hub" ]; then env_set HOUSE_HUB "$hub"; say "HOUSE_HUB=$hub"
            else TODO="$TODO
  HOUSE_HUB=      the house's door for robots, e.g. ws://192.0.2.10:8210"; fi ;;
        *) say "HOUSE_HUB is already set ($hub)" ;;
    esac
    case $(env_get HOUSE_TOKEN) in
        ""|change-me-to-a-long-random-string|ponki-in-a-box)
            token=
            [ "$ASK" = yes ] && token=$(clean "$(ask_secret "The house's HOUSE_TOKEN (from its .env; typed, not shown; Enter to skip): ")")
            if [ -n "$token" ]; then env_set HOUSE_TOKEN "$token"; say "HOUSE_TOKEN saved"
            else TODO="$TODO
  HOUSE_TOKEN=    the same token as in the house's .env"; fi ;;
        *) say "HOUSE_TOKEN is already set" ;;
    esac
fi

# -- the demo house ---------------------------------------------------------------

if [ "$MODE" = house ]; then
    step "The house"
    if [ -e data/registry.json ] || [ -e data/.seeded ]; then
        say "keeping the house in data/"
    else
        mkdir -p data
        cp -R demo/hub/. data/
        cp -R demo/workshop/. data/
        touch data/.seeded
        say "Pip, the desk camera, moved into data/ (delete data/ to start over)"
    fi
fi

# -- done -------------------------------------------------------------------------

step "Done"
if [ -n "$TODO" ]; then
    say "Still to fill in, in .env:$TODO"
    say ""
fi
if [ "$MODE" = house ]; then
    say "Start the house (Ctrl-C stops it):"
    say "  .venv/bin/python tools/start.py"
    say "then open http://localhost:8200 and say hi to Pip."
    say ""
    say "Workshops on other machines join with ./setup.sh --workshop there, and"
    say "  .venv/bin/python tools/start.py --lan"
    say "here, which opens the door for robots (:8210) to your network. Their"
    say "HOUSE_TOKEN is the one in this .env."
else
    say "Start the workshop (Ctrl-C stops it):"
    say "  .venv/bin/python tools/start.py --workshop"
    say "It shows up under System → Workshops, and its devices in Add a robot."
fi
say "Real devices may need a little more (an Arduino's firmware, a camera's"
say "account): see docs/SETUP.md for each family."
