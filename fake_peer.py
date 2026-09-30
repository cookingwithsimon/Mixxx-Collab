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

from collab_bridge import CONTROLS, NET_HELLO, NET_PING, NET_PONG, NET_VALUE

CROSSFADER = CONTROLS.index(("[Master]", "crossfader"))


def main():
    p = argparse.ArgumentParser(description="Fake peer for collab_bridge.py")
    p.add_argument("--listen", type=int, default=9001, help="local UDP port")
    p.add_argument("--bridge", default="127.0.0.1:9000", help="bridge host:port")
    p.add_argument("--sweep", action="store_true", help="sweep the crossfader continuously")
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
            elif kind == NET_PING and len(data) == struct.calcsize("!BId"):
                _, _, t = struct.unpack("!BId", data)
                sock.sendto(struct.pack("!BId", NET_PONG, session, t), bridge_addr)

    threading.Thread(target=recv_loop, daemon=True).start()

    seq = 0
    last_hello = 0.0
    start = time.time()
    try:
        while True:
            now = time.time()
            if now - last_hello >= 1.0:
                sock.sendto(struct.pack("!BI", NET_HELLO, session), bridge_addr)
                last_hello = now
            if args.sweep:
                seq += 1
                value = math.sin((now - start) * 2 * math.pi / 4.0)  # -1..1 every 4 s
                sock.sendto(struct.pack("!BIIBf", NET_VALUE, session, seq, CROSSFADER, value), bridge_addr)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Stopping")


if __name__ == "__main__":
    main()
