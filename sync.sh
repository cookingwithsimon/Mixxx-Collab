#!/usr/bin/env bash
# Copies the mapping from the project folder into Mixxx's controllers folders.
# Run after MixxxCollab.js or MixxxCollab.midi.xml changes, then restart Mixxx.
set -euo pipefail
here="$(dirname "$0")"

# A native Mixxx, including the MixxxCollab build from build_linux.sh, reads
# ~/.mixxx; the Flatpak build (SteamOS, Discover) keeps its settings under
# ~/.var/app. The Flatpak folder may not exist until Mixxx has been started
# once, so check whether the app is installed.
targets=("$HOME/.mixxx/controllers")
flatpak_home="$HOME/.var/app/org.mixxx.Mixxx"
if [ -d "$flatpak_home" ] || { command -v flatpak >/dev/null && flatpak info org.mixxx.Mixxx >/dev/null 2>&1; }; then
    targets+=("$flatpak_home/.mixxx/controllers")
fi

for dir in "${targets[@]}"; do
    mkdir -p "$dir"
    cp "$here/MixxxCollab.js" "$here/MixxxCollab.midi.xml" "$dir/"
    echo "Mapping copied to $dir"
done
