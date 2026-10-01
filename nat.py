"""Getting two bridges talking across home routers.

- STUN: ask a public server which address our UDP socket appears from.
- Invite and reply codes: short strings the DJs paste to each other in chat,
  carrying each side's public and LAN address and a per-session secret.
- UPnP: ask the leader's router to forward our UDP port, when it allows it.
- Packet signing: every packet carries a short HMAC with the session secret,
  so nobody else on the internet can drive our Mixxx.
"""

import base64
import hashlib
import hmac
import ipaddress
import os
import socket
import struct

STUN_SERVERS = [("stun.l.google.com", 19302), ("stun.cloudflare.com", 3478)]
STUN_MAGIC = 0x2112A442
TAG_LEN = 8                    # bytes of HMAC-SHA256 appended to every packet
SECRET_LEN = 16
INVITE_PREFIX = "mxc1-"
REPLY_PREFIX = "mxr1-"


# ---- packet signing ----

def sign(secret, packet):
    return packet + hmac.new(secret, packet, hashlib.sha256).digest()[:TAG_LEN]


def verify(secret, data):
    """The packet without its tag if the tag is right, else None."""
    if len(data) <= TAG_LEN:
        return None
    packet, tag = data[:-TAG_LEN], data[-TAG_LEN:]
    good = hmac.new(secret, packet, hashlib.sha256).digest()[:TAG_LEN]
    return packet if hmac.compare_digest(tag, good) else None


# ---- STUN (RFC 5389 binding request, just enough to learn our address) ----

def stun_request():
    txn = os.urandom(12)
    return struct.pack("!HHI", 0x0001, 0, STUN_MAGIC) + txn, txn


def is_stun(data):
    return len(data) >= 20 and struct.unpack_from("!I", data, 4)[0] == STUN_MAGIC


def parse_stun_response(data, txn):
    """(ip, port) from a binding success response for transaction txn, else None."""
    if not is_stun(data) or data[8:20] != txn or struct.unpack_from("!H", data)[0] != 0x0101:
        return None
    length = struct.unpack_from("!H", data, 2)[0]
    pos = 20
    while pos + 4 <= 20 + length:
        attr, alen = struct.unpack_from("!HH", data, pos)
        value = data[pos + 4:pos + 4 + alen]
        if attr in (0x0020, 0x0001) and len(value) >= 8 and value[1] == 0x01:   # IPv4
            port = struct.unpack_from("!H", value, 2)[0]
            raw = struct.unpack_from("!I", value, 4)[0]
            if attr == 0x0020:        # XOR-MAPPED-ADDRESS
                port ^= STUN_MAGIC >> 16
                raw ^= STUN_MAGIC
            return str(ipaddress.IPv4Address(raw)), port
        pos += 4 + alen + (-alen % 4)
    return None


def stun_servers():
    """Resolved STUN server addresses (skipping any that don't resolve)."""
    out = []
    for host, port in STUN_SERVERS:
        try:
            out.append((socket.gethostbyname(host), port))
        except OSError:
            pass
    return out


# ---- addresses ----

def lan_address():
    """Our address on the local network (the one used to reach the internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _pack_addr(addr):
    ip, port = addr if addr else ("0.0.0.0", 0)
    return socket.inet_aton(ip) + struct.pack("!H", port)


def _unpack_addr(raw):
    ip, port = socket.inet_ntoa(raw[:4]), struct.unpack("!H", raw[4:6])[0]
    return None if port == 0 else (ip, port)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- invite and reply codes ----

def make_invite(secret, public, lan):
    return INVITE_PREFIX + _b64(secret + _pack_addr(public) + _pack_addr(lan))


def read_invite(code):
    """(secret, [candidate addresses]) from an invite code."""
    code = code.strip()
    if not code.startswith(INVITE_PREFIX):
        raise ValueError("not a MixxxCollab invite code (they start with mxc1-)")
    raw = _unb64(code[len(INVITE_PREFIX):])
    if len(raw) != SECRET_LEN + 12:
        raise ValueError("invite code is the wrong length; was it copied whole?")
    secret = raw[:SECRET_LEN]
    return secret, [a for a in (_unpack_addr(raw[16:22]), _unpack_addr(raw[22:28])) if a]


def make_reply(secret, public, lan):
    body = _pack_addr(public) + _pack_addr(lan)
    return REPLY_PREFIX + _b64(body + hmac.new(secret, body, hashlib.sha256).digest()[:6])


def read_reply(secret, code):
    """[candidate addresses] from a reply code made for this session's invite."""
    code = code.strip()
    if not code.startswith(REPLY_PREFIX):
        raise ValueError("not a MixxxCollab reply code (they start with mxr1-)")
    raw = _unb64(code[len(REPLY_PREFIX):])
    if len(raw) != 18:
        raise ValueError("reply code is the wrong length; was it copied whole?")
    body, tag = raw[:12], raw[12:]
    if not hmac.compare_digest(tag, hmac.new(secret, body, hashlib.sha256).digest()[:6]):
        raise ValueError("that reply code belongs to a different invite")
    return [a for a in (_unpack_addr(body[:6]), _unpack_addr(body[6:12])) if a]


# ---- UPnP ----

def upnp_forward(port, description="MixxxCollab"):
    """Ask the router to forward UDP port to us. Returns (mapping, external
    (ip, port)) or (None, reason). Needs the optional miniupnpc package."""
    try:
        import miniupnpc
    except ImportError:
        return None, "UPnP support isn't installed (pip install miniupnpc)"
    try:
        u = miniupnpc.UPnP()
        u.discoverdelay = 1500
        try:
            found = u.discover()
        except Exception:      # miniupnpc raises (oddly, "Success") when nothing answers
            found = 0
        if found == 0:
            return None, "no UPnP router answered (UPnP may be off on the router)"
        u.selectigd()
        external_ip = u.externalipaddress()
        u.addportmapping(port, "UDP", u.lanaddr, port, description, "")
        return u, (external_ip, port)
    except Exception as e:     # routers vary a lot; any failure just means no UPnP
        return None, f"router refused the port forward ({e})"


def upnp_remove(mapping, port):
    try:
        mapping.deleteportmapping(port, "UDP")
    except Exception:
        pass
