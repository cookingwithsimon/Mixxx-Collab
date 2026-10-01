#!/usr/bin/env bash
# Builds the MixxxCollab build of Mixxx 2.5.6 (stock Mixxx plus
# mixxx-collab.patch, which lets the mapping load tracks) on SteamOS or any
# Linux with distrobox. It builds inside an Ubuntu 24.04 container, so the
# read-only SteamOS system isn't touched, and installs to ~/mixxx-collab.
#
#   bash build_linux.sh
#
# Expect about 2 GB of packages and a 20-40 minute compile (tested on Ubuntu
# 24.04 under WSL; the container uses the same Ubuntu release). Safe to rerun:
# it picks up where it stopped.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
box=mixxx-build
src="$HOME/mixxx-collab-src"
prefix="$HOME/mixxx-collab"

if ! command -v distrobox >/dev/null; then
    echo "distrobox isn't installed. SteamOS 3.5 and later include it; on other"
    echo "distros install distrobox and podman first."
    exit 1
fi

if ! distrobox list | grep -q "| $box "; then
    echo "== Creating the $box container (Ubuntu 24.04)"
    distrobox create --yes --name "$box" --image docker.io/library/ubuntu:24.04
fi

# Everything below runs inside the container, which shares your home folder.
distrobox enter "$box" -- bash -c "
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null; then
    sudo apt-get update
    # pipewire-alsa: sound from inside the container goes to SteamOS's PipeWire.
    sudo apt-get install -y git pipewire-alsa libasound2-plugins
fi
if [ ! -d '$src/.git' ]; then
    echo '== Fetching Mixxx 2.5.6 and applying the MixxxCollab patch'
    git clone --depth 1 --branch 2.5.6 https://github.com/mixxxdj/mixxx.git '$src'
    git -C '$src' apply '$here/mixxx-collab.patch'
fi
if [ ! -f /var/tmp/mixxx-deps-done ]; then
    echo '== Installing build dependencies'
    cd '$src' && yes n | ./tools/debian_buildenv.sh setup
    sudo touch /var/tmp/mixxx-deps-done
fi
echo '== Building (this is the long part)'
cmake -S '$src' -B '$src/build' -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_INSTALL_PREFIX='$prefix' -DWARNINGS_FATAL=OFF \
    -DBUILD_TESTING=OFF -DBUILD_BENCH=OFF
cmake --build '$src/build' --parallel \$(nproc)
cmake --install '$src/build'
"
echo
echo "Done. Start the bridge first, then this Mixxx with:  bash run_mixxx_linux.sh"
