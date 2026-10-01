"""MixxxCollab bridge — proof of concept.

Sits between a local Mixxx (via a loopMIDI virtual port running the
MixxxCollab Bridge mapping) and a remote peer running the same thing.

    Mixxx <--SysEx over loopMIDI--> collab_bridge.py <--UDP--> peer's collab_bridge.py <--> peer's Mixxx

Usage:
    python collab_bridge.py --midi "MixxxCollab" --listen 9000 --peer 192.168.1.50:9000 --leader
    python collab_bridge.py --midi "MixxxCollab" --listen 9000 --peer 192.168.1.20:9000

Exactly one side should pass --leader. When the peers connect (or one restarts),
the leader pushes its full mixer state so both sides start out matching.
"""

import argparse
import collections
import json
import os
import heapq
import itertools
import random
import socket
import statistics
import struct
import sys
import threading
import time

import mido

import filesync
import nat
import panel

DECKS = [f"[Channel{n}]" for n in range(1, 5)]   # must match MixxxCollab.decks

# Must match MixxxCollab.controls in MixxxCollab.js: built the same way, and
# the index (under 128) is what goes over the wire.
CONTROLS = [("[Master]", "crossfader")]
for _g in DECKS:
    CONTROLS += [
        (_g, "play"), (_g, "volume"), (_g, "pregain"), (_g, "rate"), (_g, "keylock"),
        (f"[EqualizerRack1_{_g}_Effect1]", "parameter1"),
        (f"[EqualizerRack1_{_g}_Effect1]", "parameter2"),
        (f"[EqualizerRack1_{_g}_Effect1]", "parameter3"),
        (f"[QuickEffectRack1_{_g}]", "loaded_chain_preset"),
        (f"[QuickEffectRack1_{_g}]", "super1"),
        (f"[QuickEffectRack1_{_g}]", "enabled"),
        (_g, "loop_start_position"), (_g, "loop_end_position"), (_g, "loop_enabled"),
    ]
for _u in (1, 2):
    _unit = f"[EffectRack1_EffectUnit{_u}]"
    CONTROLS += [(_unit, "loaded_chain_preset"), (_unit, "mix"), (_unit, "super1"), (_unit, "enabled")]
    CONTROLS += [(_unit, f"group_{_g}_enable") for _g in DECKS]
    for _e in (1, 2, 3):
        CONTROLS += [(f"[EffectRack1_EffectUnit{_u}_Effect{_e}]", "loaded_effect"),
                     (f"[EffectRack1_EffectUnit{_u}_Effect{_e}]", "enabled"),
                     (f"[EffectRack1_EffectUnit{_u}_Effect{_e}]", "meta")]
CONTROLS += [("[Master]", "gain")]     # master output level
assert len(CONTROLS) < 128
# The crossfader and master gain have one writer at a time: whoever holds the
# control token (see "The control token" in the protocol spec).
SHARED_IDX = {CONTROLS.index(("[Master]", "crossfader")), CONTROLS.index(("[Master]", "gain"))}
PLAY_IDX = {i for i, (_, key) in enumerate(CONTROLS) if key == "play"}

# SysEx protocol (see MixxxCollab.js)
SYSEX_ID = 0x7D
FROM_MIXXX = 0x01
TO_MIXXX = 0x02
MSG_VALUE = 0x01
MSG_SNAPSHOT_REQUEST = 0x02
MSG_POSITION = 0x03   # from Mixxx: idx = deck, value = playposition (0..1)
MSG_SEEK = 0x04       # to Mixxx:   idx = deck, value = playposition to jump to
MSG_TRIM = 0x05       # to Mixxx:   idx = deck, value = relative speed trim
MSG_SPEED = 0x06      # from Mixxx: idx = deck, value = playback speed in track fractions per second
MSG_LOOP = 0x07       # from Mixxx: idx = deck, value = active loop length as a track fraction, 0 if none
# Track paths travel in chunks: F0 7D <dir> <type> <deck> <chunk> <count> <nibbles...> F7,
# each byte of the UTF-8 path as two 4-bit nibbles (SysEx bytes must stay under 0x80,
# and loopMIDI caps a SysEx message at 256 bytes).
MSG_LOADED = 0x08     # from Mixxx: the file now loaded on a deck (needs the MixxxCollab Mixxx build)
MSG_LOAD = 0x09       # to Mixxx:   load this file on a deck
MSG_REPORT_TRACKS = 0x0A  # to Mixxx: report every deck's loaded file again
MSG_FOLLOW = 0x0B     # to Mixxx:   idx = deck, value = 1 if this side follows the other's playhead
PATH_CHUNK = 96       # path bytes per SysEx message
# Control idx of each deck's play button, in deck order.
DECK_PLAY_IDX = [CONTROLS.index((group, "play")) for group in DECKS]

# UDP protocol
# Control values carry the session time of the change. Newest wins on both
# sides, and each side resends the values it last changed every second, so a
# lost or late packet repairs itself and a stale one can't undo a newer move.
NET_VALUE = 1     # !BIBfd   type, session, idx, value, session time of the change
VALUE_FMT = "!BIBfd"
NET_POSITION = 5  # !BIBddBd type, session, deck, session time, playposition, playing, speed
POSITION_FMT = "!BIBddBd"
NET_HELLO = 2   # !BI     type, session
NET_TOKEN = 11    # !BIIBd   leader -> follower: token epoch, holder (0 leader, 1 follower), session time it moved
NET_TOKEN_REQ = 12  # !BIBI  follower -> leader: 1 take / 2 hand over, epoch the follower last saw
TOKEN_FMT = "!BIIBd"
TOKEN_REQ_FMT = "!BIBI"
TAKE, HAND_OVER = 1, 2
NET_LOAD = 6     # !BIBd + UTF-8 path: type, session, deck, session time of the load,
                 # path relative to the shared music folder, with / separators
LOAD_FMT = "!BIBd"
NET_PING = 3    # !BId    type, session, t0 (sender's local clock)
NET_PONG = 4    # !BIddd  type, session, echoed t0, t1 (received), t2 (replied)
PING_FMT = "!BId"
PONG_FMT = "!BIddd"

HELLO_INTERVAL = 1.0
PEER_TIMEOUT = 5.0
RESEND_INTERVAL = 1.0     # resend our latest control values and paused positions this often
PUNCH_INTERVAL = 0.25     # while not connected, say hello to every candidate address this often
STUN_INTERVAL = 20.0      # refresh our public address (and keep the router's mapping open)
CONNECT_HINT_AFTER = 15.0  # explain what to try if an internet session hasn't connected by then
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".mixxxcollab", "session.json")

# Clock sync (see "Clock sync" in the protocol doc). Starting values to tune.
JOIN_BURST = 8            # pings sent when a peer (re)connects
BURST_INTERVAL = 0.05
PING_INTERVAL = 2.0
SYNC_WINDOW = 150         # samples kept (5 minutes); long enough to average out Wi-Fi jitter
LOCK_SAMPLES = 8          # locked when this many recent offsets...
LOCK_SPREAD = 0.002       # ...agree within this many seconds
MAX_SLEW = 500e-6         # max correction rate while playing (500 ppm)
STEP_THRESHOLD = 0.05     # step instead of slewing above this, if nothing plays
DRIFT_MIN_SPAN = 20.0     # seconds of samples needed before estimating drift
MAX_DRIFT = 200e-6        # ignore implausible drift estimates beyond 200 ppm
STATUS_INTERVAL = 10.0

# Deck sync (see "Late events and reconciliation" in the protocol doc).
LEADER_HISTORY = 3.0      # seconds of leader position reports used to predict its playhead
JUMP_THRESHOLD = 0.03     # a leader report this far off the prediction means it seeked
ERROR_MEDIAN = 7          # position errors the correction is based on
SEEK_THRESHOLD = 0.05     # seek when further out than this; nudge the rate otherwise
SEEK_LEAD = 0.03          # starting guess for how long a seek takes to reach Mixxx's engine
SEEK_HOLD = 0.5           # ignore our own position reports this long after seeking
TRIM_TIME = 2.5           # nudge so the error would close in about this many seconds
MAX_TRIM = 0.005          # never change speed by more than 0.5%
TRIM_RESEND = 1.0         # resend the trim this often so the mapping knows it's live
MAX_SEEK_LEAD = 0.3       # bounds for the learned seek lead
START_MATCH = 0.5         # a leader start anchor this close to the play event belongs to it
PAUSED_TOLERANCE = 0.02   # while both are paused, line up positions further apart than this
PAUSE_SETTLE = 0.75       # ...but only once the owner's paused position has stopped moving
LOAD_SETTLE = 3.0         # after a track loads, leave the deck alone this long: Mixxx
                          # itself moves it (to the cue point, after analysis)
REFINE_TIME = 4.0         # just after a start, seeks are cheap; for this long...
REFINE_THRESHOLD = 0.005  # ...seek for errors above this instead of trimming for seconds...
REFINE_SEEKS = 3          # ...at most this many times
LATE_START = 0.05         # only jump ahead on play if it reached us at least this late
MAX_LEAD_STEP = 0.02      # one landing can move the learned seek lead at most this much


def encode_float(value):
    u = struct.unpack(">I", struct.pack(">f", value))[0]
    return [(u >> 28) & 0x0F, (u >> 21) & 0x7F, (u >> 14) & 0x7F, (u >> 7) & 0x7F, u & 0x7F]


def decode_float(b):
    u = (b[0] << 28) | (b[1] << 21) | (b[2] << 14) | (b[3] << 7) | b[4]
    return struct.unpack(">f", struct.pack(">I", u & 0xFFFFFFFF))[0]


def find_port(names, wanted, kind):
    for name in names:
        if wanted.lower() in name.lower():
            return name
    print(f"No MIDI {kind} port matching '{wanted}'. Available: {names}")
    sys.exit(1)


class Impairment:
    """Testing only: make the network worse on purpose, in both directions.

    Spec is comma-separated, e.g. "delay=20,jitter=10,loss=2,spike=500/20":
    delay/jitter in ms, loss in percent, and spike=MS/EVERY_S stalls all
    traffic for MS milliseconds every EVERY_S seconds, then lets it through in
    a burst, like a Wi-Fi hiccup. Jitter reorders packets too.
    """

    def __init__(self, spec):
        self.delay = self.jitter = self.loss = 0.0
        self.spike_len = self.spike_every = 0.0
        for part in spec.split(","):
            key, _, val = part.partition("=")
            if key == "delay":
                self.delay = float(val) / 1000
            elif key == "jitter":
                self.jitter = float(val) / 1000
            elif key == "loss":
                self.loss = float(val) / 100
            elif key == "spike":
                length, _, every = val.partition("/")
                self.spike_len, self.spike_every = float(length) / 1000, float(every)
            else:
                raise ValueError(f"unknown impairment '{key}' in '{spec}'")
        self.heap = []
        self.order = itertools.count()
        self.cond = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def schedule(self, action):
        """Run action() after this packet's simulated delay, or never if it is lost."""
        if random.random() < self.loss:
            return
        now = time.monotonic()
        delay = self.delay + random.uniform(0, self.jitter)
        if self.spike_every:
            phase = now % self.spike_every
            if phase < self.spike_len:
                delay += self.spike_len - phase   # held until the stall ends
        with self.cond:
            heapq.heappush(self.heap, (now + delay, next(self.order), action))
            self.cond.notify()

    def _run(self):
        while True:
            with self.cond:
                while not self.heap:
                    self.cond.wait()
                wait = self.heap[0][0] - time.monotonic()
                if wait > 0:
                    self.cond.wait(wait)
                    continue
                _, _, action = heapq.heappop(self.heap)
            action()


class SessionClock:
    """Session time shared by both peers.

    The leader's local clock is the session clock. The follower estimates its
    offset from ping-pong samples, trusting the ones with the shortest round
    trip, and slews towards that estimate so session time doesn't jump while a
    deck is playing.
    """

    def __init__(self, leader, skew=0.0, drift_ppm=0.0):
        self.leader = leader
        # Testing only: pretend this machine's clock is offset / runs fast.
        self.skew = skew
        self.rate = 1.0 + drift_ppm * 1e-6
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=SYNC_WINDOW)  # (rtt, offset, local time)
        self.offset = 0.0     # add to local time to get session time
        self.drift = 0.0      # estimated rate difference between the two clocks
        self.fit_spread = None  # scatter of the samples the drift fit is built from
        self.synced = leader  # offset has been set from a full burst (the leader is the reference)
        self.last_slew = self.local()

    def local(self):
        return time.perf_counter() * self.rate + self.skew

    def to_session(self, local_time):
        with self.lock:
            return local_time + self.offset

    def now(self):
        return self.to_session(self.local())

    def reset(self):
        with self.lock:
            self.samples.clear()
            self.synced = self.leader

    def add_sample(self, t0, t1, t2, t3):
        """t0/t3: our local send/receive times; t1/t2: peer's receive/reply times."""
        rtt = (t3 - t0) - (t2 - t1)
        offset = ((t1 - t0) + (t2 - t3)) / 2
        with self.lock:
            self.samples.append((rtt, offset, t3))
        return rtt

    def _target(self, now):
        # Use the quarter of samples with the lowest round trip. Two crystals
        # differ by tens of ppm, so over the sample window the true offset
        # moves; fit a line through those samples and extrapolate to now
        # rather than taking a median that lags behind.
        best = sorted(self.samples)[:max(1, len(self.samples) // 4)]
        offsets = [o for _, o, _ in best]
        times = [t for _, _, t in best]
        self.drift = 0.0
        self.fit_spread = None
        if len(best) < 4 or max(times) - min(times) < DRIFT_MIN_SPAN:
            # Not enough history for a fit yet: trust the best recent sample,
            # so stale burst samples can't hold the estimate still.
            return min(list(self.samples)[-LOCK_SAMPLES:])[1]
        t_mean = statistics.fmean(times)
        o_mean = statistics.fmean(offsets)
        var = sum((t - t_mean) ** 2 for t in times)
        slope = sum((t - t_mean) * (o - o_mean) for t, o in zip(times, offsets)) / var
        self.drift = max(-MAX_DRIFT, min(MAX_DRIFT, slope))
        residuals = [o - o_mean - self.drift * (t - t_mean) for t, o in zip(times, offsets)]
        self.fit_spread = max(residuals) - min(residuals)
        return o_mean + self.drift * (now - t_mean)

    def slew(self, playing):
        with self.lock:
            now = self.local()
            dt = now - self.last_slew
            self.last_slew = now
            if self.leader or len(self.samples) < LOCK_SAMPLES:
                return
            error = self._target(now) - self.offset
            if not self.synced or (abs(error) > STEP_THRESHOLD and not playing):
                self.offset += error
                self.synced = True
            else:
                limit = MAX_SLEW * dt
                self.offset += max(-limit, min(limit, error))

    def status(self):
        with self.lock:
            if not self.samples:
                return None
            target_error = self._target(self.local()) - self.offset
            # Judge agreement on the recent samples with drift removed, and
            # without the two slowest round trips, so that one delayed packet
            # or ordinary crystal drift doesn't read as losing lock.
            recent = sorted(list(self.samples)[-LOCK_SAMPLES:])[:-2]
            recent = [o - self.drift * t for _, o, t in recent]
            if self.fit_spread is not None:
                # Once there is a fit, lock means the low-round-trip samples
                # it is built from agree; raw samples on Wi-Fi scatter by
                # several ms even when the estimate is steady.
                agree = self.fit_spread < LOCK_SPREAD
            else:
                agree = len(recent) == LOCK_SAMPLES - 2 and max(recent) - min(recent) < LOCK_SPREAD
            return {
                "rtt": min(r for r, _, _ in self.samples),
                "offset": self.offset,
                "error": target_error,
                "drift": self.drift,
                "locked": self.synced and agree,
            }


class DeckSync:
    """Keeps one local deck on the leader's playhead (runs on the follower).

    Both Mixxx instances report each deck's playposition a few times a second,
    and their bridges stamp the reports with session time. From the leader's
    recent reports we predict where its playhead is at any session time, and
    compare our own reports against that. Far out: seek. Close: nudge the
    playback rate until the error closes.
    """

    def __init__(self, deck, seek, trim):
        self.deck = deck
        self.seek = seek          # seek(deck, playposition)
        self.trim = trim          # trim(deck, relative speed change)
        self.lock = threading.Lock()
        self.leader = collections.deque()                 # (session time, position)
        self.errors = collections.deque(maxlen=ERROR_MEDIAN)
        self.hold_until = 0.0
        self.current_trim = 0.0
        self.trim_sent = 0.0
        self.last_error = None    # seconds; positive means we are ahead
        self.seeks = 0
        self.leader_reports = 0
        self.own_reports = 0
        self.leader_speed = 0.0       # track fractions per second, from the leader's mapping
        self.leader_paused = None     # (session time, position) while the leader is paused
        self.start_anchor = None      # (session time, position) where the leader last started
        self.pending_start = None     # session time of a play we received but haven't anchored
        self.own_last = None          # (session time, position, playing) of our latest report
        self.leader_latest = float("-inf")  # newest leader report time seen
        self.loop_len = 0.0           # our active loop, as a track fraction (0: none)
        self.seek_lead = SEEK_LEAD     # learned from where our seeks actually land
        self.check_landing = False     # next error measurement tells us how a seek landed
        self.refine_until = 0.0        # session time until which small errors still get a seek
        self.refine_left = 0
        self.settle_until = 0.0        # session time until which a newly loaded deck is left alone

    def _predict(self, t):
        """Leader's position and speed (track fraction per second) at session time t."""
        if len(self.leader) < 3:
            return None
        t_mean = statistics.fmean(a for a, _ in self.leader)
        p_mean = statistics.fmean(p for _, p in self.leader)
        var = sum((a - t_mean) ** 2 for a, _ in self.leader)
        if var == 0:
            return None
        speed = sum((a - t_mean) * (p - p_mean) for a, p in self.leader) / var
        if speed <= 0:
            return None
        return p_mean + speed * (t - t_mean), speed

    def leader_report(self, t, pos, playing, speed, now):
        with self.lock:
            self.leader_reports += 1
            if speed > 0:
                self.leader_speed = speed
            stale = t < self.leader_latest   # overtaken by a newer report (reordered or resent)
            self.leader_latest = max(self.leader_latest, t)
            if not playing:
                if stale:
                    return
                self.leader_paused = (t, pos)
                self.leader.clear()
                self.errors.clear()
                self.last_error = None
                self._align_paused(now)
                return
            fresh = not self.leader or t - self.leader[-1][0] > 1.0
            if not stale and (self.leader_paused is not None or fresh):
                # First report since the leader pressed play (or since we last
                # heard from it): where it started.
                self.start_anchor = (t, pos)
                self.leader_paused = None
                self.leader.clear()
                self._catch_up_start(now)
            predicted = self._predict(t)
            if predicted and abs(pos - predicted[0]) / predicted[1] > JUMP_THRESHOLD:
                # The owner jumped (hotcue, cue, beatjump, seek) or its loop
                # wrapped around: start a new history. A loop wrap happens
                # here too, on its own, so only follow a real jump straight
                # away rather than waiting for the error to build up.
                jump = abs(pos - predicted[0])
                wrapped = self.loop_len > 0 and abs(jump - self.loop_len) / predicted[1] < JUMP_THRESHOLD
                self.leader.clear()
                self.errors.clear()
                if not wrapped and self.own_last is not None and self.own_last[2]:
                    self._jump_to(t, pos, now)
            self.leader.append((t, pos))
            while t - self.leader[0][0] > LEADER_HISTORY:
                self.leader.popleft()

    def started(self, t_play, now):
        """We just applied the leader's play, which it pressed at session time t_play."""
        with self.lock:
            self.pending_start = t_play
            self._catch_up_start(now)

    def _catch_up_start(self, now):
        # A play that reaches us late would start the deck behind the leader
        # and leave it to the trims (or a seek a second later) to catch up.
        # Jump straight to where the leader is by now instead, using the
        # position it started from. The listener hears a clipped start, never
        # a misaligned one.
        if self.pending_start is None or self.start_anchor is None or not self.leader_speed:
            return
        t0, pos0 = self.start_anchor
        if abs(t0 - self.pending_start) > START_MATCH:
            return
        self.pending_start = None
        # Seeks right after a start aren't noticeable, so allow a few quick
        # ones to settle the deck instead of trimming for seconds.
        self.refine_until = now + REFINE_TIME
        self.refine_left = REFINE_SEEKS
        if now - t0 >= LATE_START:
            self._jump_to(t0, pos0, now)

    def _jump_to(self, t0, pos0, now):
        # Go to where the owner is by now, given it was at pos0 at session
        # time t0. Seeks issued as a deck starts or jumps land less
        # predictably (seen on the Ally), so this doesn't teach us the seek
        # lead; the quick refinements that follow fix it up.
        self.refine_until = now + REFINE_TIME
        self.refine_left = REFINE_SEEKS
        self._seek_to(pos0 + self.leader_speed * (now - t0 + self.seek_lead), now, learn=False)

    def _seek_to(self, target, now, learn=True):
        self.seek(self.deck, min(max(target, 0.0), 1.0))
        self.seeks += 1
        self.errors.clear()
        self.hold_until = now + SEEK_HOLD
        self.check_landing = learn

    def track_changed(self, now):
        """A track was just loaded on this deck, here or on the other side."""
        with self.lock:
            self.settle_until = now + LOAD_SETTLE
            self.leader.clear()
            self.errors.clear()
            self.last_error = None

    def _align_paused(self, now):
        # While both decks are paused, keep them on the same spot, so a pause
        # that arrived late or a cue press is already lined up for the next
        # play. Seeking a paused deck is silent. Right after a load, and
        # while the owner's deck is still moving (Mixxx jumping to the cue
        # point once analysis finishes), wait: chasing each of those moves
        # made the deck bounce. The owner resends its paused position every
        # second, so this runs again once things are quiet.
        if self.leader_paused is None or self.own_last is None or self.own_last[2]:
            return
        if not self.leader_speed or now < self.hold_until or now < self.settle_until:
            return
        if now - self.leader_paused[0] < PAUSE_SETTLE:
            return
        target = self.leader_paused[1]
        if abs(self.own_last[1] - target) / self.leader_speed > PAUSED_TOLERANCE:
            self.seek(self.deck, target)
            self.seeks += 1
            self.own_last = (now, target, False)
            self.hold_until = now + SEEK_HOLD

    def own_report(self, t, pos, playing):
        with self.lock:
            self.own_reports += 1
            self.own_last = (t, pos, playing)
            if not playing:
                self.errors.clear()
                self.last_error = None
                self._align_paused(t)
                return
            if t < self.hold_until:
                return
            if not self.leader or t - self.leader[-1][0] > 1.0:
                self.errors.clear()   # leader isn't playing this deck
                self.last_error = None
                return
            predicted = self._predict(t)
            if predicted is None:
                return
            expected, speed = predicted
            diff = pos - expected
            if self.loop_len > 0:
                # Both decks loop the same section, so compare positions
                # around the loop: just before and just after its start are
                # close, not a loop length apart.
                diff = (diff + self.loop_len / 2) % self.loop_len - self.loop_len / 2
            self.errors.append(diff / speed)
            if len(self.errors) < 3:
                return
            error = statistics.median(self.errors)
            self.last_error = error
            if self.check_landing:
                # Where the last seek landed tells us how long seeks take to
                # reach the engine; learn it, half a step at a time.
                self.check_landing = False
                step = max(-MAX_LEAD_STEP, min(MAX_LEAD_STEP, error / 2))
                self.seek_lead = max(0.0, min(MAX_SEEK_LEAD, self.seek_lead - step))
            refining = t < self.refine_until and self.refine_left > 0
            if abs(error) > (REFINE_THRESHOLD if refining else SEEK_THRESHOLD):
                self._seek_to(expected + speed * self.seek_lead, t)
                if refining:
                    self.refine_left -= 1
                else:
                    # A big jump is like a fresh start: allow refinements again.
                    self.refine_until = t + REFINE_TIME
                    self.refine_left = REFINE_SEEKS
                return
            trim = max(-MAX_TRIM, min(MAX_TRIM, -error / TRIM_TIME))
            if abs(trim - self.current_trim) > 5e-6 or t - self.trim_sent > TRIM_RESEND:
                self.current_trim = trim
                self.trim_sent = t
                self.trim(self.deck, trim)


def session_ok(data, peer_session):
    """File packets carry no session id of their own (0); they're accepted
    once a peer is connected, and they're signed on internet sessions."""
    return peer_session is not None and len(data) >= 9


def chunk_path(kind, deck, path):
    """SysEx data messages carrying path to/from Mixxx, nibble-encoded in chunks."""
    raw = path.encode("utf-8")
    chunks = [raw[i:i + PATH_CHUNK] for i in range(0, len(raw), PATH_CHUNK)] or [b""]
    return [[SYSEX_ID, TO_MIXXX, kind, deck, n, len(chunks)]
            + [nib for byte in chunk for nib in (byte >> 4, byte & 0x0F)]
            for n, chunk in enumerate(chunks)]


class Bridge:
    def __init__(self, args):
        self.args = args
        self.leader = args.leader
        self.verbose = args.verbose
        self.clock = SessionClock(args.leader, args.test_clock_skew, args.test_clock_drift)
        self.clock_test = bool(args.test_clock_skew or args.test_clock_drift)
        self.clock_log = None
        if args.clock_log:
            self.clock_log = open(args.clock_log, "w")
            self.clock_log.write("local_time,rtt,raw_offset,applied_offset,drift\n")
        self.burst_left = 0
        self.pending_pings = set()  # t0 of pings not yet answered
        # Each deck has an owner whose playhead is authoritative; we correct
        # the decks the other machine owns.
        self.positions_sent = 0
        if args.own_decks:
            self.owned = {int(n) - 1 for n in args.own_decks.split(",")}
        else:
            self.owned = {0, 1} if args.leader else {2, 3}
        self.deck_sync = {}
        if not args.no_deck_sync:
            self.deck_sync = {d: DeckSync(d, self.send_seek, self.send_trim)
                              for d in range(len(DECKS)) if d not in self.owned}
        self.sync_log = None
        if args.sync_log:
            self.sync_log = open(args.sync_log, "w")
            self.sync_log.write("session_time,deck,error_ms,trim_ppm,seeks,seek_lead_ms\n")
        self.playing = {}  # control idx -> bool, for the play controls
        # idx -> {"value", "ts": session time if it came from the peer,
        #         "local": our local time if it came from our Mixxx}
        self.state = {}
        self.state_lock = threading.Lock()
        self.deck_speed = [0.0] * len(DECKS)
        self.last_position = [None] * len(DECKS)   # last NET_POSITION packet sent, per deck
        self.session = random.getrandbits(32)
        self.midi_lock = threading.Lock()
        self.impair = Impairment(args.impair) if args.impair else None
        # The leader starts with the crossfader and master token.
        self.token = {"epoch": 0, "holder": 0, "changed_at": 0.0}
        self.token_pending = None       # (action, time) while a request awaits the leader
        self.pickup = {}                # shared control idx -> which side the fader was on
        self.last_token_nag = 0.0
        self.notices = []
        self.invite_code = None         # shown on the panel
        self.reply_code = None
        # Track loading: paths travel relative to each machine's copy of the
        # shared music folder, so the same file can sit at different places.
        self.library = os.path.normpath(args.library) if args.library else None
        self.loads = {}                # deck -> {"path", "ts", "local"}, newest wins as with values
        self.deck_track = {}           # deck -> relative path our Mixxx has loaded (None: outside library)
        self.path_chunks = {}          # deck -> {chunk: bytes} while a path arrives from Mixxx
        # Copies tracks the partner loads that we don't have (and sends ours).
        self.files = None
        if self.library:
            self.files = filesync.FileSync(self.library, self.send, args.file_rate * 1e6 / 8,
                                           lambda: any(self.playing.values()))

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", args.listen))
        # Internet play: with a session secret every packet is signed, and the
        # peer is whoever signs correctly, from whichever of its candidate
        # addresses (public or LAN) gets through first. Without one (plain
        # --peer on a LAN), only the given address is trusted.
        self.secret = args.secret
        self.candidates = list(args.candidates)
        self.peer_addr = self.candidates[0] if self.candidates else None
        self.public_addr = None
        self.stun_txns = {}
        self.stun_seen = {}            # STUN server -> the public address it saw us at
        self.stun_primary = None
        self.strict_warned = False
        self.upnp = None

        self.peer_session = None
        self.peer_last_seen = 0.0

        if args.virtual:
            # Linux/macOS: no loopMIDI, so create the port ourselves. Mixxx
            # only sees it if the bridge is already running when Mixxx starts.
            if sys.platform == "win32":
                sys.exit("--virtual is not supported on Windows; use a loopMIDI port instead.")
            in_name = out_name = args.midi
            self.midi_out = mido.open_output(out_name, virtual=True, client_name=args.midi)
            self.midi_in = mido.open_input(in_name, virtual=True, client_name=args.midi,
                                           callback=self.on_midi)
        else:
            in_name = find_port(mido.get_input_names(), args.midi, "input")
            out_name = find_port(mido.get_output_names(), args.midi, "output")
            self.midi_out = mido.open_output(out_name)
            self.midi_in = mido.open_input(in_name, callback=self.on_midi)
        print(f"MIDI: in='{in_name}' out='{out_name}'")
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_REPORT_TRACKS])
        self.send_following()
        peer = ", ".join(f"{ip}:{port}" for ip, port in self.candidates) or "waiting for the partner"
        print(f"UDP:  listening on {args.listen}, peer {peer}"
              + ("  (signed: internet session)" if self.secret else ""))
        print(f"Role: {'LEADER' if self.leader else 'follower'}  session={self.session:08x}  "
              f"owns decks {', '.join(str(d + 1) for d in sorted(self.owned))}")
        if self.impair:
            print(f"TEST: impairing the network both ways: {args.impair}")

    def send(self, packet, to=None):
        """Send to the peer, or, before we've heard from it, to every
        candidate address (which is also what opens a path through routers)."""
        if self.secret:
            packet = nat.sign(self.secret, packet)
        targets = [to] if to else ([self.peer_addr] if self.connected() or not self.candidates
                                   else list(self.candidates))
        for addr in targets:
            if addr is None:
                continue
            if self.impair:
                self.impair.schedule(lambda a=addr: self._sendto(packet, a))
            else:
                self._sendto(packet, addr)

    def _sendto(self, packet, addr):
        try:
            self.sock.sendto(packet, addr)
        except OSError:
            pass   # e.g. no route yet; punching keeps trying

    # ---- reaching the peer across the internet ----
    def stun(self):
        for server in nat.stun_servers():
            request, txn = nat.stun_request()
            self.stun_txns[txn] = server
            self._sendto(request, server)

    def on_stun(self, data):
        for txn in list(self.stun_txns):
            addr = nat.parse_stun_response(data, txn)
            if addr:
                server = self.stun_txns.pop(txn)
                before = self.stun_seen.get(server)
                self.stun_seen[server] = addr
                self.check_strict_nat()
                # On a strict network each STUN server sees a different port,
                # so compare like with like: only a change seen by the same
                # server is a real change, and our address in codes is the
                # one the first server to answer saw.
                if before is not None and addr != before:
                    print(f"Our public address changed to {addr[0]}:{addr[1]}")
                if self.public_addr is None or server == self.stun_primary:
                    self.stun_primary = server
                    self.public_addr = addr
                return

    def check_strict_nat(self):
        """Two STUN servers seeing us at different public ports means our
        router (or mobile network) picks a new port for every destination.
        Then the port in our codes is only right for the STUN server, and a
        direct connection only works if the other side's port is open."""
        ports = {addr for addr in self.stun_seen.values()}
        if len(ports) > 1 and not self.strict_warned:
            self.strict_warned = True
            seen = ", ".join(f"{ip}:{port}" for ip, port in sorted(ports))
            print("\nNOTE: this network is a strict one (it shows a different public port to each\n"
                  f"      server: {seen}; common on mobile data). Connecting will only work if the\n"
                  "      other side's port is open: the leader can forward a UDP port on its\n"
                  "      router and start with --listen <port> (see the README).\n")

    def connect_hint(self):
        if self.leader:
            return ("Not connected yet. If the partner's reply code hasn't been used, paste it now. If it\n"
                    "  has, one of the two networks is probably strict: forward a UDP port on this router\n"
                    f"  to this machine, restart with --invite --listen <port>, and send the new invite.")
        return ("Not connected yet. Send the reply code above to the leader if you haven't. If you have,\n"
                "  one of the two networks is probably strict: ask the leader to forward a UDP port on\n"
                "  their router and send a new invite (see the README).")

    def wait_for_public_address(self, timeout=3.0):
        self.stun()
        end = time.time() + timeout
        while self.public_addr is None and time.time() < end:
            time.sleep(0.05)
        return self.public_addr

    def add_candidates(self, addrs):
        for addr in addrs:
            if addr not in self.candidates:
                self.candidates.append(addr)
        if self.peer_addr is None and self.candidates:
            self.peer_addr = self.candidates[0]

    def save_session(self):
        if not self.secret:
            return
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        known = ([self.peer_addr] if self.peer_addr else []) + self.candidates
        unique = [list(a) for i, a in enumerate(known) if a and a not in known[:i]]
        with open(SESSION_FILE, "w") as f:
            json.dump({"secret": self.secret.hex(), "leader": self.leader, "peer": unique}, f)

    # ---- Mixxx -> network ----
    def on_midi(self, msg):
        if msg.type != "sysex":
            return
        d = msg.data  # excludes F0 / F7
        if len(d) < 3 or d[0] != SYSEX_ID or d[1] != FROM_MIXXX:
            return  # not ours, or our own TO_MIXXX message looping back
        if d[2] == MSG_LOADED and len(d) >= 6:
            self.on_loaded_chunk(d[3], d[4], d[5], bytes(d[6:]))
            return
        if len(d) < 9:
            return
        idx = d[3]
        value = decode_float(d[4:9])
        if d[2] == MSG_POSITION:
            self.on_position(idx, value)
            return
        if d[2] == MSG_SPEED:
            if idx < len(DECKS):
                self.deck_speed[idx] = value
            return
        if d[2] == MSG_LOOP:
            if idx in self.deck_sync:
                self.deck_sync[idx].loop_len = max(0.0, value)
            return
        if d[2] != MSG_VALUE or idx >= len(CONTROLS):
            return
        if idx in SHARED_IDX and not self.allow_shared_move(idx, value):
            return
        if idx in PLAY_IDX:
            self.playing[idx] = value > 0.5
        with self.state_lock:
            self.state[idx] = {"value": value, "ts": None, "local": self.clock.local()}
        self.send_value(idx)
        if self.verbose:
            g, k = CONTROLS[idx]
            print(f"  -> {g},{k} = {value:.4f}")

    def entry_time(self, entry):
        """Session time of a state entry. Ours are kept in local time until
        first sent, then frozen: our clock offset keeps adjusting, and a
        resend must carry exactly the same time or the peer would take it as
        a newer change (and, say, re-apply an old pause)."""
        if entry["ts"] is not None:
            return entry["ts"]
        return self.clock.to_session(entry["local"])

    def send_value(self, idx):
        """Send our latest value for idx, if the latest change was ours."""
        if not (self.leader or self.clock.synced):
            return  # our timestamps aren't comparable yet; the resend loop will catch up
        with self.state_lock:
            entry = self.state.get(idx)
            if entry is None or entry["local"] is None:
                return
            entry["ts"] = self.entry_time(entry)
            packet = struct.pack(VALUE_FMT, NET_VALUE, self.session, idx, entry["value"], entry["ts"])
        self.send(packet)

    def on_position(self, deck, pos):
        """Our Mixxx reported a deck's playposition; stamp it with session time."""
        if deck >= len(DECKS):
            return
        t = self.clock.now()
        playing = bool(self.playing.get(DECK_PLAY_IDX[deck]))
        self.positions_sent += 1
        packet = struct.pack(POSITION_FMT, NET_POSITION, self.session, deck, t, pos,
                             playing, self.deck_speed[deck])
        self.last_position[deck] = packet
        self.send(packet)
        sync = self.deck_sync.get(deck)
        if sync and self.clock.synced and self.same_track(deck):
            sync.own_report(t, pos, playing)
            if self.sync_log and sync.last_error is not None:
                self.sync_log.write(f"{t:.4f},{deck + 1},{sync.last_error * 1000:.3f},"
                                    f"{sync.current_trim * 1e6:.1f},{sync.seeks},{sync.seek_lead * 1000:.1f}\n")
                self.sync_log.flush()

    # ---- track loading ----
    def to_relative(self, location):
        """Path of a local file relative to the shared folder, or None if outside it."""
        if not self.library or not location:
            return None
        full = os.path.normpath(location)
        try:
            rel = os.path.relpath(full, self.library)
        except ValueError:          # different drive on Windows
            return None
        if rel.startswith(".."):
            return None
        return rel.replace(os.sep, "/")

    def on_loaded_chunk(self, deck, n, count, nibbles):
        if deck >= len(DECKS):
            return
        parts = self.path_chunks.setdefault(deck, {})
        if n == 0:
            parts.clear()
        parts[n] = bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles) - 1, 2))
        if len(parts) < count:
            return
        location = b"".join(parts[i] for i in range(count)).decode("utf-8", "replace")
        parts.clear()
        self.on_track_loaded(deck, location)

    def on_track_loaded(self, deck, location):
        """Our Mixxx loaded location on deck (by a local action or because we asked it to)."""
        rel = self.to_relative(location)
        if self.deck_track.get(deck) != rel and deck in self.deck_sync:
            self.deck_sync[deck].track_changed(self.clock.now())
        self.deck_track[deck] = rel
        if not location:
            return
        if rel is None:
            print(f"Deck {deck + 1}: loaded a track outside the shared folder; "
                  f"the other side can't load it: {location}")
            return
        with self.state_lock:
            entry = self.loads.get(deck)
            if entry is not None and entry["path"] == rel:
                return      # the load we asked for (or one we already sent)
            if self.connected():
                self.loads[deck] = {"path": rel, "ts": None, "local": self.clock.local()}
            else:
                # Already loaded before the session started: an old change,
                # so any real load wins, and at connect the leader's tracks
                # win over the follower's, as with the rest of the state.
                self.loads[deck] = {"path": rel, "ts": 1.0 if self.leader else 0.0,
                                    "local": self.clock.local()}
        print(f"Deck {deck + 1}: loaded {rel}")
        self.send_load(deck)

    def send_load(self, deck):
        if not (self.leader or self.clock.synced):
            return
        with self.state_lock:
            entry = self.loads.get(deck)
            if entry is None or entry["local"] is None:
                return
            entry["ts"] = self.entry_time(entry)
            packet = struct.pack(LOAD_FMT, NET_LOAD, self.session, deck, entry["ts"]) + entry["path"].encode("utf-8")
        self.send(packet)

    def on_remote_load(self, deck, ts, rel):
        if deck >= len(DECKS):
            return
        with self.state_lock:
            entry = self.loads.get(deck)
            if entry is not None and ts <= self.entry_time(entry):
                return
            self.loads[deck] = {"path": rel, "ts": ts, "local": None}
        if not self.library:
            print(f"Deck {deck + 1}: other side loaded {rel}, but no --library is set here")
            return
        location = filesync.safe_path(self.library, rel)
        if location is None:
            print(f"Deck {deck + 1}: other side loaded {rel}, which isn't a path inside the music folder")
            return
        if not os.path.exists(location):
            if self.files:
                print(f"Deck {deck + 1}: other side loaded {rel}, missing here; fetching it")
                self.files.fetch(rel, self.on_fetched)
            return
        self.load_locally(deck, rel, location)

    def on_fetched(self, rel):
        """A fetched file arrived: load it on every deck that should have it."""
        with self.state_lock:
            decks = [d for d, entry in self.loads.items() if entry["path"] == rel]
        for deck in decks:
            self.load_locally(deck, rel, filesync.safe_path(self.library, rel))

    def load_locally(self, deck, rel, location):
        if self.deck_track.get(deck) == rel:
            return
        print(f"Deck {deck + 1}: loading {rel} to match the other side")
        if deck in self.deck_sync:
            self.deck_sync[deck].track_changed(self.clock.now())
        for data in chunk_path(MSG_LOAD, deck, location):
            self.send_to_mixxx(data)

    def same_track(self, deck):
        """False when we know the two decks hold different tracks (don't sync them)."""
        mine = self.deck_track.get(deck, "unknown")
        agreed = self.loads.get(deck)
        if mine == "unknown" or agreed is None:
            return True     # stock Mixxx can't tell us; assume the DJs loaded the same file
        return mine == agreed["path"]

    # ---- network -> Mixxx ----
    def send_to_mixxx(self, data):
        with self.midi_lock:
            self.midi_out.send(mido.Message("sysex", data=data))

    def send_following(self):
        """Tell the mapping which decks follow the other side, so it can keep
        Mixxx's own beat snapping (quantize) off on them. Repeated every
        second, in case Mixxx was restarted."""
        for deck in range(len(DECKS)):
            self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_FOLLOW, deck]
                               + encode_float(1.0 if deck in self.deck_sync else 0.0))

    def send_seek(self, deck, pos):
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_SEEK, deck] + encode_float(pos))
        print(f"  Deck {deck + 1}: seek to match leader")

    def send_trim(self, deck, trim):
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_TRIM, deck] + encode_float(trim))

    def request_snapshot(self):
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_SNAPSHOT_REQUEST])

    # ---- the control token for the crossfader and master gain ----
    def i_hold_token(self):
        return (self.token["holder"] == 0) == self.leader

    def agreed_value(self, idx):
        with self.state_lock:
            entry = self.state.get(idx)
        return None if entry is None else entry["value"]

    def allow_shared_move(self, idx, value):
        """Our DJ moved the crossfader or master gain. Without the token the
        move is undone; just after taking it, the move only counts once the
        physical control reaches the current position (soft takeover), so
        taking control never makes the mix jump."""
        agreed = self.agreed_value(idx)
        if not self.i_hold_token():
            if agreed is not None:
                self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_VALUE, idx] + encode_float(agreed))
            now = time.time()
            if now - self.last_token_nag > 5:
                self.last_token_nag = now
                self.notice(f"{self.partner_name()} has the crossfader and master; press Take control to use them")
            return False
        if idx in self.pickup and agreed is not None:
            side = value > agreed
            if abs(value - agreed) > 0.04 and self.pickup[idx] in (None, side):
                self.pickup[idx] = side
                self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_VALUE, idx] + encode_float(agreed))
                return False
            del self.pickup[idx]      # crossed or reached it: it's live
        return True

    def set_token(self, epoch, holder, changed_at):
        before = self.i_hold_token()
        self.token = {"epoch": epoch, "holder": holder, "changed_at": changed_at}
        self.token_pending = None
        if self.i_hold_token() and not before:
            self.pickup = {idx: None for idx in SHARED_IDX}
            self.notice("You have the crossfader and master")
        elif before and not self.i_hold_token():
            self.pickup = {}
            self.notice(f"{self.partner_name()} has the crossfader and master now")

    def broadcast_token(self):
        t = self.token
        self.send(struct.pack(TOKEN_FMT, NET_TOKEN, self.session, t["epoch"], t["holder"], t["changed_at"]))

    def token_action(self, action):
        """The DJ pressed Take control (TAKE) or Hand over (HAND_OVER)."""
        if self.leader:
            # The leader arbitrates, so its own presses take effect at once.
            want = 0 if action == TAKE else 1
            if self.token["holder"] != want:
                self.set_token(self.token["epoch"] + 1, want, self.clock.now())
                self.broadcast_token()
        else:
            self.token_pending = (action, time.time())
            self.send(struct.pack(TOKEN_REQ_FMT, NET_TOKEN_REQ, self.session, action, self.token["epoch"]))

    def on_token_request(self, action, epoch_seen):
        if not self.leader:
            return
        if epoch_seen == self.token["epoch"]:
            want = 1 if action == TAKE else 0
            if self.token["holder"] != want:
                self.set_token(self.token["epoch"] + 1, want, self.clock.now())
        # Otherwise the follower acted on an old state (we both pressed at
        # once): the leader's state stands, and resending it tells them.
        self.broadcast_token()

    def on_token(self, epoch, holder, changed_at):
        if self.leader:
            return
        pending = self.token_pending
        if epoch > self.token["epoch"] or (pending and epoch == self.token["epoch"]):
            moved = epoch > self.token["epoch"]
            self.set_token(epoch, holder, changed_at)
            if pending and not moved:
                self.notice(f"{self.partner_name()} kept the crossfader and master")

    # ---- the session panel ----
    def use_reply(self, code):
        try:
            addrs, matches = nat.read_reply(self.secret, code)
        except (ValueError, TypeError) as e:
            self.notice(f"Reply code not accepted: {e}")
            return False
        self.add_candidates(addrs)
        self.notice("Reply code accepted; reaching the partner at "
                    + ", ".join(f"{ip}:{port}" for ip, port in addrs))
        if not matches:
            self.notice("(its check characters don't match this session's invite; trying anyway)")
        return True

    def deck_status(self, deck, connected):
        """(text, colour) for one deck on the panel."""
        with self.state_lock:
            load = self.loads.get(deck)
        track = load["path"] if load else self.deck_track.get(deck)
        if not track or track == "unknown":
            return "Empty", ""
        if self.files and track in self.files.incoming:
            inc = self.files.incoming[track]
            done = 100 * inc.have // inc.chunks if inc.chunks else 0
            return f"Fetching from partner ({done}%)", "amber"
        if not self.same_track(deck):
            return "Different track on each side", "red"
        playing = self.playing.get(DECK_PLAY_IDX[deck])
        if deck in self.owned:
            return ("Playing" if playing else "Paused"), "green"
        if not connected:
            return "Partner offline", "red"
        if not playing:
            return "Paused", "green"
        sync = self.deck_sync.get(deck)
        if sync is None or sync.last_error is None:
            return "Lining up", "amber"
        error = sync.last_error * 1000
        if abs(error) < 5:
            return "In sync", "green"
        return f"Correcting ({error:+.0f} ms)", "amber"

    def panel_state(self):
        now = time.time()
        connected = self.connected()
        status = self.clock.status() if connected else None
        decks = []
        for d in range(len(DECKS)):
            text, colour = self.deck_status(d, connected)
            with self.state_lock:
                load = self.loads.get(d)
            track = load["path"] if load else self.deck_track.get(d)
            name = track.rsplit("/", 1)[-1].rsplit(".", 1)[0] if track and track != "unknown" else None
            decks.append({"deck": d + 1, "mine": d in self.owned, "state": text, "colour": colour,
                          "track": name})
        with self.state_lock:
            notices = [{"ago": panel.ago(now - t), "text": text} for t, text in self.notices]
        return {
            "connected": connected,
            "last_seen_s": (now - self.peer_last_seen) if self.peer_session is not None else None,
            "rtt_ms": status["rtt"] * 1000 if status else None,
            "clock_locked": bool(self.leader or (status and status["locked"])),
            "leader": self.leader,
            "internet": bool(self.secret),
            "token": {"mine": self.i_hold_token(), "pending": self.token_pending is not None},
            "decks": decks,
            "invite": self.invite_code if not connected else None,
            "reply": self.reply_code if not connected else None,
            "wants_reply": bool(self.secret and self.leader and not connected),
            "notices": notices,
        }

    def panel_action(self, name, body):
        if name == "take" and self.connected() and not self.i_hold_token():
            self.token_action(TAKE)
            return True
        if name == "handover" and self.connected() and self.i_hold_token():
            self.token_action(HAND_OVER)
            return True
        if name == "reply" and self.secret and isinstance(body.get("code"), str):
            return self.use_reply(body["code"])
        return False

    # ---- notices for the panel ----
    def notice(self, text):
        print(f"* {text}")
        with self.state_lock:
            self.notices.append((time.time(), text))
            del self.notices[:-20]

    def partner_name(self):
        return "The partner"

    def note_peer(self, session):
        now = time.time()
        was_connected = self.peer_session is not None and now - self.peer_last_seen < PEER_TIMEOUT
        new_session = session != self.peer_session
        self.peer_last_seen = now
        if new_session:
            self.peer_session = session
        if new_session or not was_connected:
            print(f"Peer connected (session={session:08x})")
            self.notice("Partner connected")
            self.clock.reset()
            self.burst_left = JOIN_BURST
            if self.leader:
                print("  Leader: pushing full mixer state to peer")
                self.request_snapshot()

    def recv_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except ConnectionResetError:
                continue  # Windows raises this for ICMP port-unreachable; ignore
            try:
                self.on_datagram(data, addr)
            except Exception as e:
                # With the port open to the internet, anything can arrive; a
                # bad packet must never stop us receiving.
                print(f"Ignored a malformed packet from {addr[0]}:{addr[1]} ({e.__class__.__name__})")

    def on_datagram(self, data, addr):
        """One UDP packet: STUN answers, then (signed) packets from the peer."""
        if not data:
            return
        if nat.is_stun(data):
            if addr in self.stun_txns.values():   # only answers from servers we asked
                self.on_stun(data)
            return
        if self.secret:
            data = nat.verify(self.secret, data)
            if data is None:
                return  # not signed with our session's secret
            if addr != self.peer_addr:
                print(f"Peer reached at {addr[0]}:{addr[1]}")
                self.peer_addr = addr
                self.add_candidates([addr])
                self.save_session()
        elif addr != self.peer_addr:
            return  # only the configured peer may drive this Mixxx
        if self.impair:
            # Arrival time is taken when the impaired packet is delivered.
            self.impair.schedule(lambda d=data: self.handle_packet(d, self.clock.local()))
        else:
            self.handle_packet(data, self.clock.local())

    def on_value(self, idx, value, ts):
        """A control value from the peer, changed at session time ts."""
        if idx >= len(CONTROLS):
            return
        if idx in SHARED_IDX and (self.i_hold_token() or ts < self.token["changed_at"]):
            return  # from a DJ who doesn't (or didn't then) hold the crossfader and master
        with self.state_lock:
            entry = self.state.get(idx)
            if entry is not None and ts <= self.entry_time(entry):
                return  # we already have this change, or a newer one
            self.state[idx] = {"value": value, "ts": ts, "local": None}
        if idx in PLAY_IDX:
            self.playing[idx] = value > 0.5
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_VALUE, idx] + encode_float(value))
        if idx in DECK_PLAY_IDX and value > 0.5 and self.clock.synced:
            sync = self.deck_sync.get(DECK_PLAY_IDX.index(idx))
            if sync:
                sync.started(ts, self.clock.now())
        if self.verbose:
            g, k = CONTROLS[idx]
            late = (self.clock.now() - ts) * 1000
            print(f"  <- {g},{k} = {value:.4f}" + (f"  ({late:.0f} ms late)" if late > 50 else ""))

    def handle_packet(self, data, received):
        kind = data[0]
        if kind == NET_VALUE and len(data) == struct.calcsize(VALUE_FMT):
            _, session, idx, value, ts = struct.unpack(VALUE_FMT, data)
            self.note_peer(session)
            self.on_value(idx, value, ts)
        elif kind == NET_POSITION and len(data) == struct.calcsize(POSITION_FMT):
            _, session, deck, t, pos, playing, speed = struct.unpack(POSITION_FMT, data)
            sync = self.deck_sync.get(deck)
            if session == self.peer_session and sync and self.clock.synced and self.same_track(deck):
                sync.leader_report(t, pos, bool(playing), speed, self.clock.now())
        elif kind in (filesync.NET_FILE_REQ, filesync.NET_FILE_INFO, filesync.NET_FILE_DATA,
                      filesync.NET_FILE_NEED):
            if self.files and session_ok(data, self.peer_session):
                {filesync.NET_FILE_REQ: self.files.on_request, filesync.NET_FILE_INFO: self.files.on_info,
                 filesync.NET_FILE_DATA: self.files.on_data, filesync.NET_FILE_NEED: self.files.on_need,
                 }[kind](data)
        elif kind == NET_LOAD and len(data) > struct.calcsize(LOAD_FMT):
            _, session, deck, ts = struct.unpack_from(LOAD_FMT, data)
            self.note_peer(session)
            self.on_remote_load(deck, ts, data[struct.calcsize(LOAD_FMT):].decode("utf-8", "replace"))
        elif kind == NET_TOKEN and len(data) == struct.calcsize(TOKEN_FMT):
            _, session, epoch, holder, changed_at = struct.unpack(TOKEN_FMT, data)
            self.note_peer(session)
            self.on_token(epoch, holder, changed_at)
        elif kind == NET_TOKEN_REQ and len(data) == struct.calcsize(TOKEN_REQ_FMT):
            _, session, action, epoch_seen = struct.unpack(TOKEN_REQ_FMT, data)
            self.note_peer(session)
            self.on_token_request(action, epoch_seen)
        elif kind == NET_HELLO and len(data) == struct.calcsize("!BI"):
            _, session = struct.unpack("!BI", data)
            self.note_peer(session)
        elif kind == NET_PING and len(data) == struct.calcsize(PING_FMT):
            _, session, t0 = struct.unpack(PING_FMT, data)
            self.note_peer(session)
            t1 = self.clock.to_session(received)
            self.send(struct.pack(PONG_FMT, NET_PONG, self.session, t0, t1, self.clock.now()))
        elif kind == NET_PONG and len(data) == struct.calcsize(PONG_FMT):
            _, session, t0, t1, t2 = struct.unpack(PONG_FMT, data)
            if session != self.peer_session or t0 not in self.pending_pings:
                return  # not an answer to one of our pings
            self.pending_pings.discard(t0)
            rtt = self.clock.add_sample(t0, t1, t2, received)
            if self.clock_log:
                raw_offset = ((t1 - t0) + (t2 - received)) / 2
                self.clock_log.write(f"{received:.6f},{rtt:.6f},{raw_offset:.6f},"
                                     f"{self.clock.offset:.6f},{self.clock.drift:.9f}\n")
                self.clock_log.flush()

    def connected(self):
        return self.peer_session is not None and time.time() - self.peer_last_seen < PEER_TIMEOUT

    def hello_loop(self):
        was_connected = False
        last_hello = last_stun = 0.0
        trying_since = time.time()       # when we started (or went back to) trying to connect
        hinted = False
        while True:
            now = time.time()
            connected = self.connected()
            # Hello once a second when connected; four times a second to every
            # candidate address while we're still trying to get through.
            if now - last_hello >= (min(HELLO_INTERVAL, RESEND_INTERVAL) if connected else PUNCH_INTERVAL):
                self.send(struct.pack("!BI", NET_HELLO, self.session))
                if connected:
                    self.resend()
                last_hello = now
            if self.secret and now - last_stun >= STUN_INTERVAL:
                self.stun()
                last_stun = now
            if connected:
                trying_since, hinted = now, False
            elif self.secret and not hinted and now - trying_since > CONNECT_HINT_AFTER:
                print(self.connect_hint())
                hinted = True
            if was_connected and not connected:
                print("Peer lost; trying to reach it again")
                self.notice("Partner lost; trying to reach them again")
            was_connected = connected
            time.sleep(0.05)

    def resend_token(self):
        if self.leader:
            self.broadcast_token()
        elif self.token_pending and time.time() - self.token_pending[1] > 2.0:
            self.token_pending = None
            self.notice("No answer from the leader about the crossfader; try again")

    def resend(self):
        """Repeat what the peer might have missed: our latest control values,
        and where each paused deck sits (playing decks report continuously)."""
        with self.state_lock:
            ours = [idx for idx, entry in self.state.items() if entry["local"] is not None]
        for idx in ours:
            self.send_value(idx)
        self.send_following()
        self.resend_token()
        with self.state_lock:
            our_loads = [deck for deck, entry in self.loads.items() if entry["local"] is not None]
        for deck in our_loads:
            self.send_load(deck)
        for deck, packet in enumerate(self.last_position):
            if packet is not None and not self.playing.get(DECK_PLAY_IDX[deck]):
                self.send(packet)

    def clock_loop(self):
        next_ping = 0.0
        next_status = 0.0
        was_locked = False
        while True:
            now = time.monotonic()
            if self.connected() and now >= next_ping:
                t0 = self.clock.local()
                if len(self.pending_pings) > 64:
                    self.pending_pings.clear()  # unanswered pings; forget them
                self.pending_pings.add(t0)
                self.send(struct.pack(PING_FMT, NET_PING, self.session, t0))
                if self.burst_left > 0:
                    self.burst_left -= 1
                    next_ping = now + BURST_INTERVAL
                else:
                    next_ping = now + PING_INTERVAL
            self.clock.slew(any(self.playing.values()))

            s = self.clock.status() if self.connected() else None
            if s:
                locked = s["locked"]
                if self.leader:
                    if now >= next_status:
                        print(f"RTT {s['rtt'] * 1000:.2f} ms  session time {self.clock.now():.3f}  "
                              f"deck positions sent {self.positions_sent}")
                        self.print_deck_sync()
                        next_status = now + STATUS_INTERVAL
                elif locked != was_locked or now >= next_status:
                    line = (f"Clock {'LOCKED' if locked else 'syncing'}: "
                            f"offset {s['offset'] * 1000:+.3f} ms, "
                            f"drift {s['drift'] * 1e6:+.1f} ppm, "
                            f"error {s['error'] * 1000:+.3f} ms, "
                            f"RTT {s['rtt'] * 1000:.2f} ms, "
                            f"session time {self.clock.now():.3f}")
                    if self.clock_test:
                        # Same-machine test: the leader's clock is perf_counter.
                        true_error = self.clock.now() - time.perf_counter()
                        line += f", TRUE error {true_error * 1000:+.3f} ms"
                    print(line)
                    self.print_deck_sync()
                    next_status = now + STATUS_INTERVAL
                was_locked = locked
            time.sleep(0.02)

    def print_deck_sync(self):
        for sync in self.deck_sync.values():
            if not self.playing.get(DECK_PLAY_IDX[sync.deck]):
                continue
            # The report counts show which side is silent when sync isn't
            # happening: ours (mapping) or the deck owner's.
            counts = f"reports: owner {sync.leader_reports}, own {sync.own_reports}"
            if sync.last_error is None:
                print(f"Deck {sync.deck + 1} sync: NOT ACTIVE, {counts}")
            else:
                print(f"Deck {sync.deck + 1} sync: error {sync.last_error * 1000:+.2f} ms, "
                      f"trim {sync.current_trim * 1e6:+.0f} ppm, seeks {sync.seeks}, {counts}")

    def run(self, recv_started=False):
        if not recv_started:
            threading.Thread(target=self.recv_loop, daemon=True).start()
        threading.Thread(target=self.hello_loop, daemon=True).start()
        threading.Thread(target=self.clock_loop, daemon=True).start()
        print("Bridge running. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping")
        finally:
            if self.upnp:
                nat.upnp_remove(self.upnp, self.args.listen)
            self.midi_in.close()
            self.midi_out.close()


def main():
    p = argparse.ArgumentParser(description="MixxxCollab proof-of-concept bridge")
    p.add_argument("--midi", default="MixxxCollab", help="loopMIDI port name (substring match)")
    p.add_argument("--virtual", action="store_true",
                   help="create a virtual MIDI port named --midi (Linux/macOS) instead of using an existing one")
    p.add_argument("--listen", type=int, default=9000, help="local UDP port")
    p.add_argument("--peer", help="peer host:port on the same network")
    p.add_argument("--invite", action="store_true",
                   help="leader: start an internet session and print an invite code for the partner")
    p.add_argument("--join", metavar="CODE", help="follower: join the session in this invite code")
    p.add_argument("--reply", metavar="CODE",
                   help="leader: the partner's reply code (otherwise asked for, or read from "
                        "~/.mixxxcollab/reply.txt)")
    p.add_argument("--resume", action="store_true",
                   help="reconnect the last internet session without new codes")
    p.add_argument("--no-upnp", action="store_true", help="leader: don't ask the router to forward the port")
    p.add_argument("--panel-port", type=int, default=8765, metavar="PORT",
                   help="local port for the session panel web page (0: no panel)")
    p.add_argument("--no-browser", action="store_true", help="don't open the session panel automatically")
    p.add_argument("--leader", action="store_true", help="this side's state wins on connect")
    p.add_argument("--verbose", "-v", action="store_true", help="log every control change")
    p.add_argument("--list-ports", action="store_true", help="list MIDI ports and exit")
    p.add_argument("--library", metavar="FOLDER",
                   help="this machine's path to the shared music folder; enables track loading")
    p.add_argument("--file-rate", type=float, default=10.0, metavar="MBIT",
                   help="most upload bandwidth for sending tracks to the partner, in Mbit/s "
                        "(half of it while a deck plays; default 10)")
    p.add_argument("--own-decks", metavar="LIST",
                   help="decks whose playhead this side owns, e.g. 1,2 (default: leader 1,2, follower 3,4)")
    p.add_argument("--no-deck-sync", action="store_true",
                   help="don't correct the other side's decks (for baseline measurements)")
    p.add_argument("--sync-log", metavar="FILE",
                   help="follower: write every deck position error to this CSV file")
    p.add_argument("--clock-log", metavar="FILE",
                   help="write every clock sync sample to this CSV file, for tuning the filter")
    p.add_argument("--impair", metavar="SPEC",
                   help='testing only: degrade the network both ways, e.g. "delay=20,jitter=10,'
                        'loss=2,spike=500/20" (ms, ms, percent, stall ms / every s)')
    p.add_argument("--test-clock-skew", type=float, default=0.0, metavar="SECONDS",
                   help="testing only: shift this machine's clock by this much")
    p.add_argument("--test-clock-drift", type=float, default=0.0, metavar="PPM",
                   help="testing only: make this machine's clock run fast by this much")
    args = p.parse_args()

    if args.list_ports:
        print("Inputs: ", mido.get_input_names())
        print("Outputs:", mido.get_output_names())
        return
    args.secret, args.candidates = None, []
    if args.peer:
        host, port = args.peer.rsplit(":", 1)
        args.candidates = [(socket.gethostbyname(host), int(port))]
    if args.join:
        try:
            args.secret, invite_addrs = nat.read_invite(args.join)
        except ValueError as e:
            p.error(str(e))
        args.candidates += invite_addrs
        args.leader = False
    elif args.invite:
        args.secret = os.urandom(nat.SECRET_LEN)
        args.leader = True
    elif args.resume:
        try:
            with open(SESSION_FILE) as f:
                saved = json.load(f)
        except OSError:
            p.error("no saved session to resume; start one with --invite / --join")
        args.secret = bytes.fromhex(saved["secret"])
        args.leader = saved["leader"]
        args.candidates += [tuple(a) for a in saved["peer"]]
    elif not args.peer:
        p.error("give --peer for a local session, or --invite / --join / --resume for an internet one")

    bridge = Bridge(args)
    threading.Thread(target=bridge.recv_loop, daemon=True).start()
    if args.secret:
        public = bridge.wait_for_public_address()
        lan = (nat.lan_address(), args.listen)
        print(f"Public address: {public[0]}:{public[1]}" if public else
              "Couldn't reach a STUN server; only the local network will work")
        if args.invite:
            if not args.no_upnp:
                bridge.upnp, result = nat.upnp_forward(args.listen)
                if bridge.upnp:
                    print(f"Router forwards UDP {result[0]}:{result[1]} to us (UPnP)")
                    public = result
                else:
                    print(f"No port forward: {result}. The reply code will be needed.")
            bridge.invite_code = nat.make_invite(args.secret, public, lan)
            print("\n  Invite code for your partner:\n\n    " + bridge.invite_code + "\n")
            threading.Thread(target=wait_for_reply, args=(bridge, args.reply), daemon=True).start()
        elif args.resume and args.leader:
            threading.Thread(target=wait_for_reply, args=(bridge, args.reply), daemon=True).start()
        elif args.join:
            bridge.reply_code = nat.make_reply(args.secret, public, lan)
            print("\n  Send this reply code back to the leader:\n\n    " + bridge.reply_code + "\n")
        bridge.save_session()
    if args.panel_port:
        try:
            url = panel.start(bridge, args.panel_port)
            print(f"Session panel: {url}")
            if not args.no_browser:
                import webbrowser
                webbrowser.open(url)
        except OSError as e:
            print(f"Couldn't start the session panel on port {args.panel_port}: {e}")
    bridge.run(recv_started=True)


def wait_for_reply(bridge, code):
    """The leader learns the partner's addresses from the reply code. Often
    not needed (the partner's packets get through first), so it's optional."""
    # Keeps listening for the whole session: a partner who moves networks
    # (say, onto a phone hotspot) can send a fresh reply code at any time.
    path = os.path.join(os.path.dirname(SESSION_FILE), "reply.txt")
    asking = bool(sys.stdin and sys.stdin.isatty())
    while True:
        if not code and os.path.exists(path):
            with open(path) as f:
                code = f.read().strip()
            os.remove(path)
        elif not code and asking and not bridge.connected():
            try:
                code = input("  Paste the partner's reply code (or just wait if it connects): ").strip()
            except EOFError:
                asking = False   # no keyboard (started in the background): use reply.txt
        if code:
            bridge.use_reply(code)
            code = None
        time.sleep(1)


if __name__ == "__main__":
    main()
