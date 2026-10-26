#!/bin/sh
# Keeps the house's Bluetooth speaker connected, bonded and at volume.
# Usage: bt_keepalive.sh <MAC> [volume=70%]
# Runs as ponkihouse-bt-speaker, after bluetooth + bluealsa.
set -u

MAC=${1:?usage: bt_keepalive.sh <MAC> [volume]}
VOLUME=${2:-70%}

cleanup() {
    [ -n "${AGENT_PID:-}" ] && kill -- -"$AGENT_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

bluetoothctl power on >/dev/null 2>&1 || true

# Keep a bluetoothctl session alive holding the default agent: a pairing
# only stores its bond if an agent is registered at that moment.
setsid sh -c 'echo default-agent; sleep infinity' | bluetoothctl --agent NoInputNoOutput >/dev/null 2>&1 &
AGENT_PID=$!

was_connected=""
set_volume() {
    sleep 2  # the A2DP volume control appears a moment later
    control=$(amixer -D bluealsa scontrols 2>/dev/null | grep -o "'[^']*A2DP'" | head -1 | tr -d "'")
    [ -n "$control" ] && amixer -D bluealsa sset "$control" "$VOLUME,$VOLUME" >/dev/null 2>&1
}

while true; do
    info=$(bluetoothctl info "$MAC" 2>/dev/null)
    if printf '%s' "$info" | grep -q "Connected: yes"; then
        if [ "$was_connected" != "yes" ]; then
            echo "speaker connected"
            printf '%s' "$info" | grep -q "Bonded: yes" || echo "warning: not bonded"
            set_volume
            was_connected="yes"
        fi
    else
        if [ "$was_connected" = "yes" ]; then
            echo "speaker disconnected"
            was_connected="no"
        fi
        if ! printf '%s' "$info" | grep -q "Bonded: yes"; then
            bluetoothctl remove "$MAC" >/dev/null 2>&1 || true
            timeout 12 bluetoothctl scan on >/dev/null 2>&1 || true
            if bluetoothctl devices 2>/dev/null | grep -q "$MAC"; then
                timeout 20 bluetoothctl pair "$MAC" >/dev/null 2>&1 || true
                bluetoothctl trust "$MAC" >/dev/null 2>&1 || true
            fi
        fi
        timeout 15 bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
    fi
    sleep 10
done
