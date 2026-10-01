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

## Linux

There is no loopMIDI on Linux, so the bridge creates the virtual MIDI port itself
and must be running before Mixxx starts. Install Mixxx 2.5 first, then:

    bash setup.sh
    bash run.sh --peer <other-pc-ip>:9000 -v

Then start Mixxx and enable the MixxxCollab controller as above. `sync.sh` copies
the mapping into Mixxx (native and Flatpak installs).

## Status

Working on a LAN and over Wi-Fi between Windows and SteamOS:

- Mixer controls (crossfader; play, volume, gain, rate, EQ and filter on decks 1
  and 2) mirror both ways. Every change carries its session time, the newest
  wins, and each side resends its latest values every second, so lost or late
  packets repair themselves.
- A shared session clock (ping-pong with drift tracking) and deck sync: the
  follower keeps each deck on the leader's playhead with seeks and small speed
  trims. In tests with a 600 ms stall every 7 s and 5% loss, steady playback
  stayed within 5 ms; starts settle within a few seconds.

Testing tools: `--impair` on the bridge degrades the network on purpose,
`simulate.py` runs two bridges against simulated decks, and `make_click_track.py`
plus `measure_sync.py` measure real machines through a line-in.

Not yet: decks 3 and 4, cue/loops/hotcues, compensation for each machine's
output latency, internet play (NAT traversal), and file sync.

## License

GNU General Public License, version 2 or (at your option) any later version.
See [LICENSE](LICENSE).
