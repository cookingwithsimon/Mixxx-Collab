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
import random
import socket
import statistics
import struct
import sys
import threading
import time

import mido

# Must match MixxxCollab.controls in MixxxCollab.js (same order).
CONTROLS = [
    ("[Master]", "crossfader"),
    ("[Channel1]", "play"),
    ("[Channel1]", "volume"),
    ("[Channel1]", "pregain"),
    ("[Channel1]", "rate"),
    ("[EqualizerRack1_[Channel1]_Effect1]", "parameter1"),
    ("[EqualizerRack1_[Channel1]_Effect1]", "parameter2"),
    ("[EqualizerRack1_[Channel1]_Effect1]", "parameter3"),
    ("[QuickEffectRack1_[Channel1]]", "super1"),
    ("[Channel2]", "play"),
    ("[Channel2]", "volume"),
    ("[Channel2]", "pregain"),
    ("[Channel2]", "rate"),
    ("[EqualizerRack1_[Channel2]_Effect1]", "parameter1"),
    ("[EqualizerRack1_[Channel2]_Effect1]", "parameter2"),
    ("[EqualizerRack1_[Channel2]_Effect1]", "parameter3"),
    ("[QuickEffectRack1_[Channel2]]", "super1"),
]
PLAY_IDX = {i for i, (_, key) in enumerate(CONTROLS) if key == "play"}

# SysEx protocol (see MixxxCollab.js)
SYSEX_ID = 0x7D
FROM_MIXXX = 0x01
TO_MIXXX = 0x02
MSG_VALUE = 0x01
MSG_SNAPSHOT_REQUEST = 0x02

# UDP protocol
NET_VALUE = 1   # !BIIBf  type, session, seq, idx, value
NET_HELLO = 2   # !BI     type, session
NET_PING = 3    # !BId    type, session, t0 (sender's local clock)
NET_PONG = 4    # !BIddd  type, session, echoed t0, t1 (received), t2 (replied)
PING_FMT = "!BId"
PONG_FMT = "!BIddd"

HELLO_INTERVAL = 1.0
PEER_TIMEOUT = 5.0

# Clock sync (see "Clock sync" in the protocol doc). Starting values to tune.
JOIN_BURST = 8            # pings sent when a peer (re)connects
BURST_INTERVAL = 0.05
PING_INTERVAL = 2.0
SYNC_WINDOW = 32          # samples kept
LOCK_SAMPLES = 8          # locked when this many recent offsets...
LOCK_SPREAD = 0.002       # ...agree within this many seconds
MAX_SLEW = 500e-6         # max correction rate while playing (500 ppm)
STEP_THRESHOLD = 0.05     # step instead of slewing above this, if nothing plays
DRIFT_MIN_SPAN = 20.0     # seconds of samples needed before estimating drift
MAX_DRIFT = 200e-6        # ignore implausible drift estimates beyond 200 ppm
STATUS_INTERVAL = 10.0


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
        self.synced = False   # offset has been set from a full burst
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
            self.synced = False

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
        if len(best) < 4 or max(times) - min(times) < DRIFT_MIN_SPAN:
            # Not enough history for a fit yet: trust the best recent sample,
            # so stale burst samples can't hold the estimate still.
            return min(list(self.samples)[-LOCK_SAMPLES:])[1]
        t_mean = statistics.fmean(times)
        o_mean = statistics.fmean(offsets)
        var = sum((t - t_mean) ** 2 for t in times)
        slope = sum((t - t_mean) * (o - o_mean) for t, o in zip(times, offsets)) / var
        self.drift = max(-MAX_DRIFT, min(MAX_DRIFT, slope))
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
            return {
                "rtt": min(r for r, _, _ in self.samples),
                "offset": self.offset,
                "error": target_error,
                "drift": self.drift,
                "locked": self.synced and len(recent) == LOCK_SAMPLES - 2
                          and max(recent) - min(recent) < LOCK_SPREAD,
            }


class Bridge:
    def __init__(self, args):
        self.args = args
        self.leader = args.leader
        self.verbose = args.verbose
        self.clock = SessionClock(args.leader, args.test_clock_skew, args.test_clock_drift)
        self.clock_test = bool(args.test_clock_skew or args.test_clock_drift)
        self.burst_left = 0
        self.pending_pings = set()  # t0 of pings not yet answered
        self.playing = {}  # control idx -> bool, for the play controls
        self.session = random.getrandbits(32)
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.midi_lock = threading.Lock()

        host, port = args.peer.rsplit(":", 1)
        self.peer_addr = (socket.gethostbyname(host), int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", args.listen))

        self.peer_session = None
        self.peer_last_seen = 0.0
        self.last_seq_by_idx = {}

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
        print(f"UDP:  listening on {args.listen}, peer {self.peer_addr[0]}:{self.peer_addr[1]}")
        print(f"Role: {'LEADER' if self.leader else 'follower'}  session={self.session:08x}")

    # ---- Mixxx -> network ----
    def on_midi(self, msg):
        if msg.type != "sysex":
            return
        d = msg.data  # excludes F0 / F7
        if len(d) < 3 or d[0] != SYSEX_ID or d[1] != FROM_MIXXX:
            return  # not ours, or our own TO_MIXXX message looping back
        if d[2] != MSG_VALUE or len(d) < 9:
            return
        idx = d[3]
        value = decode_float(d[4:9])
        if idx in PLAY_IDX:
            self.playing[idx] = value > 0.5
        with self.seq_lock:
            self.seq += 1
            seq = self.seq
        self.sock.sendto(struct.pack("!BIIBf", NET_VALUE, self.session, seq, idx, value), self.peer_addr)
        if self.verbose and idx < len(CONTROLS):
            g, k = CONTROLS[idx]
            print(f"  -> {g},{k} = {value:.4f}")

    # ---- network -> Mixxx ----
    def send_to_mixxx(self, data):
        with self.midi_lock:
            self.midi_out.send(mido.Message("sysex", data=data))

    def request_snapshot(self):
        self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_SNAPSHOT_REQUEST])

    def note_peer(self, session):
        now = time.time()
        was_connected = self.peer_session is not None and now - self.peer_last_seen < PEER_TIMEOUT
        new_session = session != self.peer_session
        self.peer_last_seen = now
        if new_session:
            self.peer_session = session
            self.last_seq_by_idx.clear()
        if new_session or not was_connected:
            print(f"Peer connected (session={session:08x})")
            self.clock.reset()
            self.burst_left = JOIN_BURST
            if self.leader:
                print("  Leader: pushing full mixer state to peer")
                self.request_snapshot()

    def recv_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(64)
            except ConnectionResetError:
                continue  # Windows raises this for ICMP port-unreachable; ignore
            received = self.clock.local()
            if not data or addr != self.peer_addr:
                continue  # only the configured peer may drive this Mixxx
            kind = data[0]
            if kind == NET_VALUE and len(data) == struct.calcsize("!BIIBf"):
                _, session, seq, idx, value = struct.unpack("!BIIBf", data)
                self.note_peer(session)
                if seq <= self.last_seq_by_idx.get(idx, 0):
                    continue  # out-of-order / stale
                self.last_seq_by_idx[idx] = seq
                if idx in PLAY_IDX:
                    self.playing[idx] = value > 0.5
                self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_VALUE, idx] + encode_float(value))
                if self.verbose and idx < len(CONTROLS):
                    g, k = CONTROLS[idx]
                    print(f"  <- {g},{k} = {value:.4f}")
            elif kind == NET_HELLO and len(data) == struct.calcsize("!BI"):
                _, session = struct.unpack("!BI", data)
                self.note_peer(session)
            elif kind == NET_PING and len(data) == struct.calcsize(PING_FMT):
                _, session, t0 = struct.unpack(PING_FMT, data)
                self.note_peer(session)
                t1 = self.clock.to_session(received)
                self.sock.sendto(
                    struct.pack(PONG_FMT, NET_PONG, self.session, t0, t1, self.clock.now()),
                    self.peer_addr)
            elif kind == NET_PONG and len(data) == struct.calcsize(PONG_FMT):
                _, session, t0, t1, t2 = struct.unpack(PONG_FMT, data)
                if session != self.peer_session or t0 not in self.pending_pings:
                    continue  # not an answer to one of our pings
                self.pending_pings.discard(t0)
                self.clock.add_sample(t0, t1, t2, received)

    def connected(self):
        return self.peer_session is not None and time.time() - self.peer_last_seen < PEER_TIMEOUT

    def hello_loop(self):
        was_connected = False
        while True:
            self.sock.sendto(struct.pack("!BI", NET_HELLO, self.session), self.peer_addr)
            connected = self.connected()
            if was_connected and not connected:
                print("Peer lost")
            was_connected = connected
            time.sleep(HELLO_INTERVAL)

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
                self.sock.sendto(struct.pack(PING_FMT, NET_PING, self.session, t0), self.peer_addr)
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
                        print(f"RTT {s['rtt'] * 1000:.2f} ms  session time {self.clock.now():.3f}")
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
                    next_status = now + STATUS_INTERVAL
                was_locked = locked
            time.sleep(0.02)

    def run(self):
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
            self.midi_in.close()
            self.midi_out.close()


def main():
    p = argparse.ArgumentParser(description="MixxxCollab proof-of-concept bridge")
    p.add_argument("--midi", default="MixxxCollab", help="loopMIDI port name (substring match)")
    p.add_argument("--virtual", action="store_true",
                   help="create a virtual MIDI port named --midi (Linux/macOS) instead of using an existing one")
    p.add_argument("--listen", type=int, default=9000, help="local UDP port")
    p.add_argument("--peer", help="peer host:port (required unless --list-ports)")
    p.add_argument("--leader", action="store_true", help="this side's state wins on connect")
    p.add_argument("--verbose", "-v", action="store_true", help="log every control change")
    p.add_argument("--list-ports", action="store_true", help="list MIDI ports and exit")
    p.add_argument("--test-clock-skew", type=float, default=0.0, metavar="SECONDS",
                   help="testing only: shift this machine's clock by this much")
    p.add_argument("--test-clock-drift", type=float, default=0.0, metavar="PPM",
                   help="testing only: make this machine's clock run fast by this much")
    args = p.parse_args()

    if args.list_ports:
        print("Inputs: ", mido.get_input_names())
        print("Outputs:", mido.get_output_names())
        return
    if not args.peer:
        p.error("--peer is required")

    Bridge(args).run()


if __name__ == "__main__":
    main()
