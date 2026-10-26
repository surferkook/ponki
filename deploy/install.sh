#!/bin/sh
# Install this machine's ponkihouse-* systemd units.
#
# The unit files in this folder are templates: @PONKI_DIR@ and @PONKI_USER@
# are filled in with wherever this repo is cloned and whoever runs it, so
# the same units work on the NUC (~/ponki) and the Pi (~/ponkihouse).
#
#   ./deploy/install.sh            install every unit in deploy/
#   ./deploy/install.sh ponkihouse-hub   install just one
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
USER_NAME=$(id -un)

units="$@"
if [ -z "$units" ]; then
    units=$(basename -a "$DIR"/deploy/ponkihouse-*.service)
fi

for unit in $units; do
    base=${unit%.service}  # ponkihouse-hub and ponkihouse-hub.service both work
    src="$DIR/deploy/$base.service"
    [ -f "$src" ] || { echo "no such unit template: $src" >&2; exit 1; }
    tmp=$(mktemp)
    sed -e "s|@PONKI_DIR@|$DIR|g" -e "s|@PONKI_USER@|$USER_NAME|g" "$src" > "$tmp"
    sudo install -m 644 "$tmp" "/etc/systemd/system/$base.service"
    rm -f "$tmp"
    echo "installed $base.service (repo at $DIR, user $USER_NAME)"
done

sudo systemctl daemon-reload
echo "Now enable what this machine runs, e.g.:"
echo "  sudo systemctl enable --now ponkihouse-hub"
