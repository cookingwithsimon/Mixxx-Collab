#!/usr/bin/env bash
# Starts the MixxxCollab build of Mixxx made by build_linux.sh. Start the
# bridge (run.sh) first so Mixxx sees its MIDI port. Arguments pass through to
# Mixxx, e.g. --log-flush-level debug to see the mapping's messages live.
# This build keeps its settings in ~/.mixxx, separate from the Flatpak Mixxx.
exec distrobox enter mixxx-build -- "$HOME/mixxx-collab/bin/mixxx" "$@"
