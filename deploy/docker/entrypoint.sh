#!/bin/sh
# First start of a container: copy the demo house in (Pip, the desk camera),
# once. After that the volume is yours: delete it to start over.
#
#   PONKI_SEED       a folder in the image, e.g. /ponki/demo/hub
#   PONKI_SEED_INTO  where it goes, e.g. /data/hub (a volume)
set -eu

if [ -n "${PONKI_SEED:-}" ] && [ -n "${PONKI_SEED_INTO:-}" ] \
   && [ ! -e "$PONKI_SEED_INTO/.seeded" ]; then
    mkdir -p "$PONKI_SEED_INTO"
    cp -R "$PONKI_SEED/." "$PONKI_SEED_INTO/"
    touch "$PONKI_SEED_INTO/.seeded"
    echo "seeded $PONKI_SEED_INTO from $PONKI_SEED"
fi

exec "$@"
