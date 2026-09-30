#!/usr/bin/env bash
# Runs the bridge with this machine's venv, creating the virtual MIDI port that
# Mixxx connects to. Start this before Mixxx. Arguments pass straight through:
#   bash run.sh --peer 192.168.8.212:9000 -v
exec "$HOME/.mixxxcollab/venv/bin/python" "$(dirname "$0")/collab_bridge.py" --virtual "$@"
