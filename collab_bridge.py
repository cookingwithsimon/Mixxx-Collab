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
import random
import socket
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

# SysEx protocol (see MixxxCollab.js)
SYSEX_ID = 0x7D
FROM_MIXXX = 0x01
TO_MIXXX = 0x02
MSG_VALUE = 0x01
MSG_SNAPSHOT_REQUEST = 0x02

# UDP protocol
NET_VALUE = 1   # !BIIBf  type, session, seq, idx, value
NET_HELLO = 2   # !BI     type, session
NET_PING = 3    # !BId    type, session, sent_time
NET_PONG = 4    # !BId    type, session, echoed sent_time

HELLO_INTERVAL = 1.0
PEER_TIMEOUT = 5.0
PING_INTERVAL = 5.0


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


class Bridge:
    def __init__(self, args):
        self.args = args
        self.leader = args.leader
        self.verbose = args.verbose
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
            if self.leader:
                print("  Leader: pushing full mixer state to peer")
                self.request_snapshot()

    def recv_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(64)
            except ConnectionResetError:
                continue  # Windows raises this for ICMP port-unreachable; ignore
            if not data:
                continue
            kind = data[0]
            if kind == NET_VALUE and len(data) == struct.calcsize("!BIIBf"):
                _, session, seq, idx, value = struct.unpack("!BIIBf", data)
                self.note_peer(session)
                if seq <= self.last_seq_by_idx.get(idx, 0):
                    continue  # out-of-order / stale
                self.last_seq_by_idx[idx] = seq
                self.send_to_mixxx([SYSEX_ID, TO_MIXXX, MSG_VALUE, idx] + encode_float(value))
                if self.verbose and idx < len(CONTROLS):
                    g, k = CONTROLS[idx]
                    print(f"  <- {g},{k} = {value:.4f}")
            elif kind == NET_HELLO and len(data) == struct.calcsize("!BI"):
                _, session = struct.unpack("!BI", data)
                self.note_peer(session)
            elif kind == NET_PING and len(data) == struct.calcsize("!BId"):
                _, session, t = struct.unpack("!BId", data)
                self.note_peer(session)
                self.sock.sendto(struct.pack("!BId", NET_PONG, self.session, t), self.peer_addr)
            elif kind == NET_PONG and len(data) == struct.calcsize("!BId"):
                _, session, t = struct.unpack("!BId", data)
                rtt_ms = (time.perf_counter() - t) * 1000
                print(f"RTT {rtt_ms:.1f} ms (one-way ~{rtt_ms / 2:.1f} ms)")

    def hello_loop(self):
        last_ping = 0.0
        was_connected = False
        while True:
            now = time.time()
            self.sock.sendto(struct.pack("!BI", NET_HELLO, self.session), self.peer_addr)
            if now - last_ping >= PING_INTERVAL:
                self.sock.sendto(struct.pack("!BId", NET_PING, self.session, time.perf_counter()), self.peer_addr)
                last_ping = now
            connected = self.peer_session is not None and now - self.peer_last_seen < PEER_TIMEOUT
            if was_connected and not connected:
                print("Peer lost")
            was_connected = connected
            time.sleep(HELLO_INTERVAL)

    def run(self):
        threading.Thread(target=self.recv_loop, daemon=True).start()
        threading.Thread(target=self.hello_loop, daemon=True).start()
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
    p.add_argument("--listen", type=int, default=9000, help="local UDP port")
    p.add_argument("--peer", help="peer host:port (required unless --list-ports)")
    p.add_argument("--leader", action="store_true", help="this side's state wins on connect")
    p.add_argument("--verbose", "-v", action="store_true", help="log every control change")
    p.add_argument("--list-ports", action="store_true", help="list MIDI ports and exit")
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
