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
without new codes.

If the two never connect, one side is probably on a strict network (common on
mobile data) and the leader's router doesn't do UPnP. Forward one UDP port on
the leader's router to the leader's machine and start the leader with
`--listen <that port>`; the partner can then connect from any network. Every
packet is signed, so the open port only answers to your partner. (Tested: a
ROG Ally on a phone hotspot joining through a forwarded port, about 26 ms
round trip.) A relay for when neither side can forward a port isn't built yet.

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

Working between Windows and SteamOS, on a home network and over the internet
(tested with a ROG Ally on a phone hotspot joining a Windows PC through a
forwarded port, about 26 ms round trip):

- **Controls** mirror both ways: crossfader; on all four decks play, volume,
  gain, rate, keylock, 3-band EQ, quick effect (preset, knob, on/off) and loops;
  and effect units 1 and 2 (chain preset, mix, meta knob, on/off, deck
  assignment, and each slot's effect, on/off and meta). Every change carries its
  session time, the newest wins, and each side resends its latest values every
  second, so lost or late packets repair themselves. Cue, hotcues and beatjump
  aren't sent as buttons; the jump they cause is followed as a position change.
- **Deck ownership:** each deck has an owner (by default the leader owns decks
  1 and 2, the follower 3 and 4; `--own-decks` changes it) whose playhead the
  other side follows.
- **Shared clock and deck sync:** a session clock (ping-pong with drift
  tracking) and seeks plus small speed trims keep followed decks on the owner's
  playhead. In tests with a 600 ms stall every 7 s and 5% packet loss, steady
  playback stayed within 5 ms and starts settled within a few seconds. Followed
  decks keep Mixxx's quantize off (it fought the sync), and a newly loaded deck
  is left alone for a moment while Mixxx moves it to its cue point.
- **Track loading:** loading a file from the shared music folder on either side
  loads the same file on the other, with paths relative to each machine's
  `--library` folder. Deck sync pauses on a deck whose two tracks differ. This
  needs the MixxxCollab build of Mixxx on both machines: the `collab` branch of
  [Mixxx-Collab-fork](https://github.com/cookingwithsimon/Mixxx-Collab-fork),
  which adds two scripting calls (`mixxx-collab.patch` here). Windows builds with
  that repository's `build_collab.bat`; Linux and SteamOS with `build_linux.sh`.
  Stock Mixxx still syncs everything except track loading.
- **File sync:** when the partner loads a track this machine doesn't have, the
  bridge fetches it over the same signed link, checks it against the sender's
  SHA-256, puts it in the music folder and loads it (a 10 MB track took about
  10 s to a phone hotspot). Uploads are capped by `--file-rate` (default
  10 Mbit/s), halved while a deck plays. Only tracks that get loaded are
  copied; there's no full-library sync yet.
- **Session panel:** each bridge serves a small page at http://127.0.0.1:8765/
  (opened automatically; `--panel-port`, `--no-browser`) showing the partner,
  link, clock, who holds the crossfader and master, and each deck's owner,
  track and sync state, with Take control / Hand over buttons, the invite or
  reply code, and a box for the partner's reply code. It only listens on this
  machine.
- **Crossfader and master token:** one DJ at a time moves the crossfader and
  master gain (the leader starts with it). The other DJ's moves are undone
  locally; after taking control, a fader only goes live once it reaches the
  current position, so the mix doesn't jump. The leader arbitrates, so if both
  press at once, one wins.
- **Internet sessions:** invite and reply codes, signed packets, STUN, optional
  UPnP, `--resume`, a warning on strict networks, and port forwarding as the
  fallback (see Internet sessions above).


Not yet: copying the whole library ahead of a session; a relay for when
neither side can forward a port; starting sessions and moving deck ownership
from the panel (the integration plan designs both); compensation for each machine's output latency, which only matters if both
machines' audio is heard together.

Effect and chain preset choices travel as positions in Mixxx's effect lists, so
both machines need the same effects listed in the same order (the default).

Testing tools: `--impair` on the bridge degrades the network on purpose,
`simulate.py` runs two bridges against simulated decks, and `make_click_track.py`
plus `measure_sync.py` measure real machines through a line-in.

## License

GNU General Public License, version 2 or (at your option) any later version.
See [LICENSE](LICENSE).
