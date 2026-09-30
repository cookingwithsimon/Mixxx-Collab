#!/usr/bin/env bash
# Copies the mapping from the project folder into Mixxx's controllers folder
# (native and Flatpak installs). Run after MixxxCollab.js or MixxxCollab.midi.xml
# changes, then restart Mixxx.
set -euo pipefail
here="$(dirname "$0")"

targets=("$HOME/.mixxx/controllers")
if [ -d "$HOME/.var/app/org.mixxx.Mixxx" ]; then
    targets+=("$HOME/.var/app/org.mixxx.Mixxx/.mixxx/controllers")
fi

for dir in "${targets[@]}"; do
    mkdir -p "$dir"
    cp "$here/MixxxCollab.js" "$here/MixxxCollab.midi.xml" "$dir/"
    echo "Mapping copied to $dir"
done
