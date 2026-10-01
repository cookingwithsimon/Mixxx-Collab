"""Fake peer for testing collab_bridge.py with a single Mixxx.

Speaks the bridge's UDP protocol: prints every control change the bridge sends,
and (with --sweep) moves the crossfader back and forth so you can watch the
remote -> Mixxx direction working.

Usage (bridge started with --listen 9000 --peer 127.0.0.1:9001):
    python fake_peer.py --sweep
"""

import argparse
import math
import random
import socket
import struct
import threading
import time

from collab_bridge import (CONTROLS, NET_HELLO, NET_PING, NET_PONG, NET_POSITION, NET_VALUE,
                           PING_FMT, PONG_FMT, POSITION_FMT)

CROSSFADER = CONTROLS.index(("[Master]", "crossfader"))
PLAY_DECK1 = CONTROLS.index(("[Channel1]", "play"))


def main():
    p = argparse.ArgumentParser(description="Fake peer for collab_bridge.py")
    p.add_argument("--listen", type=int, default=9001, help="local UDP port")
    p.add_argument("--bridge", default="127.0.0.1:9000", help="bridge host:port")
    p.add_argument("--sweep", action="store_true", help="sweep the crossfader continuously")
    p.add_argument("--lead-seconds", type=float, metavar="TRACK_LENGTH",
                   help="act as a leader playing a track of this length on deck 1, "
                        "so a follower bridge syncs its deck 1 to it")
    args = p.parse_args()

    host, port = args.bridge.rsplit(":", 1)
    bridge_addr = (host, int(port))
    session = random.getrandbits(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.listen))
    print(f"Fake peer on {args.listen}, bridge {bridge_addr[0]}:{bridge_addr[1]}, session={session:08x}")

    def recv_loop():
        while True:
            try:
                data, _ = sock.recvfrom(64)
            except ConnectionResetError:
                continue  # bridge not up yet
            if not data:
                continue
            kind = data[0]
            if kind == NET_VALUE and len(data) == struct.calcsize("!BIIBf"):
                _, _, seq, idx, value = struct.unpack("!BIIBf", data)
                name = ",".join(CONTROLS[idx]) if idx < len(CONTROLS) else f"idx {idx}"
                print(f"  from Mixxx: {name} = {value:.4f}  (seq {seq})")
            elif kind == NET_PING and len(data) == struct.calcsize(PING_FMT):
                _, _, t0 = struct.unpack(PING_FMT, data)
                now = time.perf_counter()
                sock.sendto(struct.pack(PONG_FMT, NET_PONG, session, t0, now, now), bridge_addr)

    threading.Thread(target=recv_loop, daemon=True).start()

    seq = 0
    last_hello = 0.0
    last_position = 0.0
    start = time.time()
    lead_start = time.perf_counter()
    try:
        while True:
            now = time.time()
            if now - last_hello >= 1.0:
                sock.sendto(struct.pack("!BI", NET_HELLO, session), bridge_addr)
                last_hello = now
                if args.lead_seconds:
                    # Keep deck 1 playing on the follower (newest seq wins).
                    seq += 1
                    sock.sendto(struct.pack("!BIIBf", NET_VALUE, session, seq, PLAY_DECK1, 1.0), bridge_addr)
            if args.lead_seconds and now - last_position >= 0.2:
                # Pretend to be a leader whose deck 1 started at the top of the
                # track when this script started. Our perf_counter is the
                # session clock, since we answer pings with it.
                last_position = now
                t = time.perf_counter()
                pos = ((t - lead_start) % args.lead_seconds) / args.lead_seconds
                sock.sendto(struct.pack(POSITION_FMT, NET_POSITION, session, 0, t, pos), bridge_addr)
            if args.sweep:
                seq += 1
                value = math.sin((now - start) * 2 * math.pi / 4.0)  # -1..1 every 4 s
                sock.sendto(struct.pack("!BIIBf", NET_VALUE, session, seq, CROSSFADER, value), bridge_addr)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Stopping")


if __name__ == "__main__":
    main()
