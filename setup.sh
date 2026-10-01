#!/usr/bin/env bash
# One-time setup for a MixxxCollab machine (Linux). Run from the project folder:
#   bash setup.sh
# Creates the Python venv under ~/.mixxxcollab and copies the mapping into
# Mixxx. Install Mixxx 2.5 yourself first (e.g. flatpak install flathub org.mixxx.Mixxx).
set -euo pipefail
cd "$(dirname "$0")"

venv="$HOME/.mixxxcollab/venv"
mkdir -p "$HOME/.mixxxcollab"

# python-rtmidi only ships prebuilt wheels for some Python versions, and a
# handheld usually has no compiler. Try the system Python first, then fall back
# to a private Python 3.12 fetched by uv (no root needed).
if python3 -m venv "$venv" 2>/dev/null &&
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --only-binary=:all: mido python-rtmidi; then
    echo "Using system Python: $("$venv/bin/python" --version)"
else
    echo "System Python can't install python-rtmidi from a wheel; fetching Python 3.12 with uv."
    rm -rf "$venv"
    if ! command -v uv >/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    uv venv --python 3.12 "$venv"
    uv pip install --python "$venv/bin/python" --only-binary=:all: mido python-rtmidi
fi

# Optional: lets the leader ask the router to forward its port for internet sessions.
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check --only-binary=:all: miniupnpc 2>/dev/null ||
    uv pip install --python "$venv/bin/python" --only-binary=:all: miniupnpc 2>/dev/null ||
    echo "UPnP support not installed (optional)"

bash sync.sh

cat <<'EOF'
Done. Order matters on Linux:
  1. Start the bridge first:  bash run.sh --peer <other-pc-ip>:9000 -v
  2. Then start Mixxx: Preferences > Controllers > MixxxCollab > Enabled,
     mapping "MixxxCollab Bridge".
If you restart the bridge, restart Mixxx too.
EOF
