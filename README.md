# MixxxCollab

Proof of concept for remote co-DJing with [Mixxx](https://mixxx.org): two people
each run Mixxx with the same tracks, and mixer moves on one machine are mirrored
on the other. Only control changes travel over the network, never audio.

    Mixxx <--SysEx over loopMIDI--> collab_bridge.py <--UDP--> peer's collab_bridge.py <--> peer's Mixxx

## What's here

- `MixxxCollab.js`, `MixxxCollab.midi.xml` - Mixxx controller mapping, assigned to a
  loopMIDI port named `MixxxCollab` (not to your DJ controller).
- `collab_bridge.py` - relays control changes to the peer over UDP.
- `fake_peer.py` - stands in for the peer when testing on one machine.
- `setup.ps1`, `run.ps1`, `sync.ps1` - Windows setup, bridge launcher, and mapping copy.
- The two `.md` design documents - the plan for the full product.

## Quick start (Windows, Mixxx 2.5)

    powershell -ExecutionPolicy Bypass -File setup.ps1

In Mixxx: Preferences > Controllers > MixxxCollab > Enabled, mapping "MixxxCollab Bridge".
Then, with exactly one side passing `--leader`:

    powershell -ExecutionPolicy Bypass -File run.ps1 --peer <other-pc-ip>:9000 --leader -v

Both PCs must allow inbound UDP 9000. Rerun `sync.ps1` and restart Mixxx after
changing the mapping files.

## Internet sessions

Between two homes, the leader starts a session and gets an invite code:

    powershell -ExecutionPolicy Bypass -File run.ps1 --invite -v --library <music folder>

The partner joins with it (Linux: `bash run.sh --join <code> ...`) and gets a reply code:

    powershell -ExecutionPolicy Bypass -File run.ps1 --join <invite code> -v --library <music folder>

Send the reply code back if the two don't connect within a few seconds; the
leader pastes it when asked. Both bridges learn their public address from a
public STUN server, the leader asks its router to forward the port (UPnP)
where it can, and both send to each other's public and local addresses until
one gets through. Every packet is signed with a secret from the invite code,
so nobody else can drive your Mixxx. `--resume` reconnects the last session
without new codes. Very strict routers (some mobile networks) may still need
a relay, which isn't built yet.

## Linux

There is no loopMIDI on Linux, so the bridge creates the virtual MIDI port itself
and must be running before Mixxx starts. Install Mixxx 2.5 first, then:

    bash setup.sh
    bash run.sh --peer <other-pc-ip>:9000 -v --library <path to the shared music folder>

Then start Mixxx and enable the MixxxCollab controller as above. `sync.sh` copies
the mapping into Mixxx (native and Flatpak installs).

For track loading you need the MixxxCollab build of Mixxx. On SteamOS (or any
Linux with distrobox), `bash build_linux.sh` builds it inside an Ubuntu 24.04
container from Mixxx 2.5.6 plus `mixxx-collab.patch`, and `bash
run_mixxx_linux.sh` starts it. It keeps its settings in `~/.mixxx`, separate
from a Flatpak Mixxx.

## Status

Working on a LAN and over Wi-Fi between Windows and SteamOS:

- Controls mirror both ways: crossfader; on all four decks play, volume, gain,
  rate, keylock, 3-band EQ, quick effect (filter) and loops; and effect units 1
  and 2 (chain preset, mix, meta knob, on/off, deck assignment, and each slot's
  effect, on/off and meta). Every change carries its session time, the newest wins, and each side
  resends its latest values every second, so lost or late packets repair
  themselves. Cue, hotcues and beatjump aren't sent as buttons; the jump they
  cause is followed as a position change.
- Each deck has an owner (by default the leader owns decks 1 and 2, the
  follower 3 and 4; `--own-decks` changes it) whose playhead the other side
  follows.
- A shared session clock (ping-pong with drift tracking) and deck sync: the
  follower keeps each deck on the leader's playhead with seeks and small speed
  trims. In tests with a 600 ms stall every 7 s and 5% loss, steady playback
  stayed within 5 ms; starts settle within a few seconds.

- Track loading (needs the MixxxCollab build of Mixxx on both machines, from
  the `collab` branch of the Mixxx fork, which adds two scripting calls):
  loading a file from the shared music folder on either side loads the same
  file on the other. Start each bridge with `--library <that machine's path to
  the shared folder>`; paths travel relative to it. Deck sync pauses on a deck
  whose two tracks differ.

Testing tools: `--impair` on the bridge degrades the network on purpose,
`simulate.py` runs two bridges against simulated decks, and `make_click_track.py`
plus `measure_sync.py` measure real machines through a line-in.

Not yet: a Linux build of the Mixxx fork (stock Mixxx still syncs everything
except track loading), compensation for each machine's output latency, internet play (NAT traversal), and file sync.
Effect and chain preset choices travel as positions in Mixxx's effect lists, so
both machines need the same effects listed in the same order (the default).

## License

GNU General Public License, version 2 or (at your option) any later version.
See [LICENSE](LICENSE).
