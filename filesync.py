"""Copying a track the partner loaded but we don't have.

When the other side loads a file that's missing here, the bridge asks the
partner for it and receives it over the same signed UDP link as everything
else, then loads it. Chunks are numbered; the receiver reports which ones it
still needs a few times a second, so lost chunks are resent and the sender
speeds up or backs off with the loss it sees. While a deck is playing the
sender stays at half its maximum rate, so a transfer doesn't swamp the link
the clock and deck sync depend on. The finished file is checked against the
sender's SHA-256 before it's moved into the music folder.
"""

import hashlib
import os
import struct
import threading
import time

NET_FILE_REQ = 7    # !BII + path:      type, session, request id; path relative to the music folder
NET_FILE_INFO = 8   # !BIIQ32sI:        request id, size (NOT_FOUND if missing), SHA-256, chunk count
NET_FILE_DATA = 9   # !BIII + bytes:    request id, chunk index, chunk
NET_FILE_NEED = 10  # !BIIIH + n x !I:  request id, chunks received in a row, then missing chunk indices
REQ_FMT = "!BII"
INFO_FMT = "!BIIQ32sI"
DATA_FMT = "!BIII"
NEED_FMT = "!BIIIH"
NOT_FOUND = 2 ** 64 - 1

CHUNK = 1100                 # bytes per packet: stays under a typical internet MTU with headers
WINDOW = 512                 # chunks the sender may run ahead of what's been received in a row
NEED_INTERVAL = 0.15         # how often the receiver reports what it still needs
MAX_MISSING = 200            # missing chunks listed per report
REORDER = 32                 # a gap only counts as missing once this many later chunks arrived
STALE = 0.3                  # ...or once nothing new has arrived for this long
LOSS_BACKOFF = 0.15          # back off only above this loss: Wi-Fi and mobile links drop a few
                             # percent of packets without being congested
START_RATE = 256 * 1024      # bytes per second to start at
MIN_RATE = 64 * 1024
GIVE_UP = 15.0               # seconds without hearing from the other side
PARTIAL_DIR = ".mixxxcollab-partial"


def safe_path(library, rel):
    """The local file for a relative path, or None if it would leave the folder."""
    if not rel or rel.startswith(("/", "\\")) or ":" in rel:
        return None
    full = os.path.normpath(os.path.join(library, *rel.split("/")))
    root = os.path.normpath(library)
    if os.path.commonpath([os.path.normcase(full), os.path.normcase(root)]) != os.path.normcase(root):
        return None
    return full


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.digest()


class Outgoing:
    """One file we're sending to the partner."""

    def __init__(self, req_id, path):
        self.req_id = req_id
        self.path = path
        self.size = os.path.getsize(path)
        self.chunks = max(1, -(-self.size // CHUNK))
        self.digest = None            # filled in by a background thread (hashing reads the file)
        self.file = open(path, "rb")
        self.next = 0                 # next new chunk to send
        self.have = 0                 # chunks the receiver has in a row
        self.resend = []              # chunks the receiver reported missing
        self.rate = START_RATE
        self.sent_since_report = 0
        self.last_heard = time.time()
        self.done = False

    def read(self, index):
        self.file.seek(index * CHUNK)
        return self.file.read(CHUNK)


class Incoming:
    """One file we're fetching from the partner."""

    def __init__(self, req_id, rel, final, partial, on_done):
        self.req_id = req_id
        self.rel = rel
        self.final = final
        self.partial = partial
        self.on_done = on_done
        self.size = None
        self.digest = None
        self.chunks = None
        self.got = set()
        self.have = 0
        self.file = None
        self.started = self.last_heard = time.time()
        self.last_progress = 0.0


class FileSync:
    def __init__(self, library, send, rate_cap, playing):
        self.library = library
        self.send = send                  # send(packet) over the signed link
        self.rate_cap = rate_cap          # bytes per second when nothing is playing
        self.playing = playing            # playing() -> True while any deck plays
        self.lock = threading.Lock()
        self.outgoing = {}                # req_id -> Outgoing
        self.incoming = {}                # rel -> Incoming
        self.next_req = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        threading.Thread(target=self._run, daemon=True).start()

    # ---- receiving side ----
    def fetch(self, rel, on_done):
        """Ask the partner for rel (relative path); on_done(rel) once it's in place."""
        final = safe_path(self.library, rel)
        if final is None:
            print(f"Not fetching {rel}: the path leaves the music folder")
            return
        with self.lock:
            if rel in self.incoming:
                return
            self.next_req = (self.next_req + 1) & 0x7FFFFFFF
            partial_dir = os.path.join(self.library, PARTIAL_DIR)
            os.makedirs(partial_dir, exist_ok=True)
            partial = os.path.join(partial_dir, f"{self.next_req}.part")
            self.incoming[rel] = Incoming(self.next_req, rel, final, partial, on_done)
        print(f"Fetching {rel} from the partner...")
        self._request(self.incoming[rel])

    def _request(self, inc):
        self.send(struct.pack(REQ_FMT, NET_FILE_REQ, 0, inc.req_id) + inc.rel.encode("utf-8"))

    def _by_req(self, req_id):
        for inc in self.incoming.values():
            if inc.req_id == req_id:
                return inc
        return None

    def on_info(self, data):
        _, _, req_id, size, digest, chunks = struct.unpack_from(INFO_FMT, data)
        with self.lock:
            inc = self._by_req(req_id)
            if inc is None or inc.size is not None:
                return
            if size == NOT_FOUND:
                print(f"The partner doesn't have {inc.rel} either")
                del self.incoming[inc.rel]
                return
            inc.size, inc.digest, inc.chunks = size, digest, chunks
            inc.file = open(inc.partial, "w+b")
            inc.file.truncate(size)
            inc.last_heard = time.time()
        print(f"  {inc.rel}: {size / 1e6:.1f} MB")

    def on_data(self, data):
        _, _, req_id, index = struct.unpack_from(DATA_FMT, data)
        chunk = data[struct.calcsize(DATA_FMT):]
        with self.lock:
            inc = self._by_req(req_id)
            if inc is None or inc.file is None or index >= inc.chunks or index in inc.got:
                return
            inc.file.seek(index * CHUNK)
            inc.file.write(chunk)
            inc.got.add(index)
            while inc.have in inc.got:
                inc.have += 1
            inc.last_heard = time.time()
            finished = inc.have == inc.chunks
        if finished:
            self._finish(inc)

    def _finish(self, inc):
        with self.lock:
            if self.incoming.get(inc.rel) is not inc:
                return
            del self.incoming[inc.rel]
        self._send_need(inc)                 # tells the sender it's complete
        inc.file.close()
        if sha256_file(inc.partial) != inc.digest:
            print(f"  {inc.rel}: arrived damaged (checksum mismatch); discarded")
            os.remove(inc.partial)
            return
        os.makedirs(os.path.dirname(inc.final), exist_ok=True)
        os.replace(inc.partial, inc.final)
        took = time.time() - inc.started
        print(f"  {inc.rel}: received and checked ({inc.size / 1e6:.1f} MB in {took:.1f} s)")
        inc.on_done(inc.rel)

    def _send_need(self, inc):
        missing = []
        if inc.chunks:
            top = max(inc.got) if inc.got else -1
            if time.time() - inc.last_heard > STALE:
                # Nothing new is arriving: everything not yet here is lost,
                # including any tail the sender thinks it already sent.
                limit = inc.chunks
            else:
                # Chunks just behind the newest one are probably still on
                # their way (packets get reordered), so don't report those yet.
                limit = top - REORDER
            for i in range(inc.have, limit):
                if i not in inc.got:
                    missing.append(i)
                    if len(missing) >= MAX_MISSING:
                        break
        self.send(struct.pack(NEED_FMT, NET_FILE_NEED, 0, inc.req_id, inc.have, len(missing))
                  + b"".join(struct.pack("!I", i) for i in missing))

    # ---- sending side ----
    def on_request(self, data):
        _, _, req_id = struct.unpack_from(REQ_FMT, data)
        rel = data[struct.calcsize(REQ_FMT):].decode("utf-8", "replace")
        with self.lock:
            out = self.outgoing.get(req_id)
        if out is None:
            path = safe_path(self.library, rel)
            if path is None or not os.path.isfile(path):
                self.send(struct.pack(INFO_FMT, NET_FILE_INFO, 0, req_id, NOT_FOUND, b"\0" * 32, 0))
                return
            out = Outgoing(req_id, path)
            with self.lock:
                self.outgoing[req_id] = out
            print(f"Sending {rel} to the partner ({out.size / 1e6:.1f} MB)")

            def hash_then_announce():
                out.digest = sha256_file(path)
                self._announce(out)
            threading.Thread(target=hash_then_announce, daemon=True).start()
        elif out.digest is not None:
            self._announce(out)              # our earlier answer was lost

    def _announce(self, out):
        self.send(struct.pack(INFO_FMT, NET_FILE_INFO, 0, out.req_id, out.size, out.digest, out.chunks))

    def on_need(self, data):
        _, _, req_id, have, count = struct.unpack_from(NEED_FMT, data)
        missing = struct.unpack_from(f"!{count}I", data, struct.calcsize(NEED_FMT))
        with self.lock:
            out = self.outgoing.get(req_id)
            if out is None:
                return
            out.last_heard = time.time()
            out.have = max(out.have, have)
            if out.have >= out.chunks:
                out.done = True
                return
            # Missing chunks the receiver has reported since the last report
            # are losses: back off; otherwise speed up a little.
            sent = max(1, out.sent_since_report)
            loss = len([i for i in missing if i not in out.resend]) / sent
            if loss > LOSS_BACKOFF:
                out.rate = max(MIN_RATE, out.rate * 0.8)
            else:
                out.rate = out.rate * 1.1
            out.sent_since_report = 0
            out.resend = list(missing)

    # ---- both ----
    def _run(self):
        last_need = 0.0
        while True:
            now = time.time()
            with self.lock:
                outs = list(self.outgoing.values())
                incs = list(self.incoming.values())
            cap = self.rate_cap / 2 if self.playing() else self.rate_cap
            for out in outs:
                if out.done or now - out.last_heard > GIVE_UP:
                    with self.lock:
                        self.outgoing.pop(out.req_id, None)
                    out.file.close()
                    if not out.done:
                        print("  Partner stopped asking for a file; gave up sending it")
                    continue
                if out.digest is None:
                    continue
                with self.lock:
                    out.rate = min(out.rate, cap)
                    budget = out.rate * 0.01 / max(1, len(outs))
                    while budget > 0:
                        if out.resend:
                            index = out.resend.pop(0)
                        elif out.next < min(out.chunks, out.have + WINDOW):
                            index = out.next
                            out.next += 1
                        else:
                            break
                        chunk = out.read(index)
                        self.send(struct.pack(DATA_FMT, NET_FILE_DATA, 0, out.req_id, index) + chunk)
                        out.sent_since_report += 1
                        budget -= len(chunk) + 40
            if now - last_need >= NEED_INTERVAL:
                last_need = now
                for inc in incs:
                    if inc.size is None:
                        if now - inc.last_heard > 1.0:
                            inc.last_heard = now
                            self._request(inc)          # request or answer lost; ask again
                        continue
                    if now - inc.last_heard > GIVE_UP:
                        print(f"  {inc.rel}: the transfer stalled; asking again")
                        inc.last_heard = now
                        self._request(inc)
                    self._send_need(inc)
                    if inc.chunks and now - inc.last_progress > 2.0:
                        inc.last_progress = now
                        print(f"  {inc.rel}: {100 * inc.have // inc.chunks}%")
            time.sleep(0.01)
