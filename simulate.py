"""End-to-end test of two bridges with simulated Mixxx instances on a bad network.

Each SimMixxx imitates what the mapping does (position/speed reports, play
values, applying seeks and trims) for one deck, with its own audio clock
drift. Two real Bridge objects talk over localhost UDP, with the leader's
--impair degrading the link both ways. We measure the true playhead
difference between the two simulated decks.

    python simulate.py none "clean network"
    python simulate.py "delay=15,jitter=20,loss=5,spike=600/7" "bad Wi-Fi"
"""
import os, sys, threading, time, types, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mido
import collab_bridge as cb

TRACK = 600.0
APPLY_DELAY = 0.02   # seek/play reach the engine this long after the message


class FakePort:
    def __init__(self, sim=None, callback=None):
        self.sim, self.callback = sim, callback
    def send(self, msg):
        self.sim.receive(list(msg.data))
    def close(self):
        pass


class SimMixxx:
    def __init__(self, name, drift_ppm):
        self.name = name
        self.rate = (1 + drift_ppm * 1e-6) / TRACK   # fractions per second at trim 0
        self.trim = 0.0
        self.pos = 10.0 / TRACK
        self.playing = False
        self.last_tick = time.perf_counter()
        self.last_report = 0.0
        self.lock = threading.RLock()
        self.port_in = None
        self.values = {}
        threading.Thread(target=self._run, daemon=True).start()

    def position(self):
        with self.lock:
            self._advance()
            return self.pos

    def _advance(self):
        now = time.perf_counter()
        if self.playing:
            self.pos += (now - self.last_tick) * self.rate * (1 + self.trim)
        self.last_tick = now

    def _emit(self, kind, idx, value):
        data = [cb.SYSEX_ID, cb.FROM_MIXXX, kind, idx] + cb.encode_float(value)
        if self.port_in and self.port_in.callback:
            self.port_in.callback(mido.Message("sysex", data=data))

    def _run(self):
        while True:
            time.sleep(0.005)
            with self.lock:
                self._advance()
                now = time.perf_counter()
                if self.playing and now - self.last_report >= 0.2:
                    self.last_report = now
                    self._emit(cb.MSG_POSITION, 0, self.pos)

    def _play_changed(self, local):
        # What the mapping does: speed, the value (only if it came from us), position.
        self._emit(cb.MSG_SPEED, 0, self.rate)
        if local:
            self._emit(cb.MSG_VALUE, cb.DECK_PLAY_IDX[0], 1.0 if self.playing else 0.0)
        self.last_report = time.perf_counter()
        self._emit(cb.MSG_POSITION, 0, self.pos)

    def press_play(self, playing):
        with self.lock:
            self._advance()
            self.playing = playing
            self._play_changed(local=True)

    def jump(self, seconds):
        with self.lock:
            self._advance()
            self.pos += seconds / TRACK

    def press_value(self, idx, value):
        with self.lock:
            self.values[idx] = value
            self._emit(cb.MSG_VALUE, idx, value)

    def receive(self, data):
        if data[2] in (cb.MSG_SNAPSHOT_REQUEST, cb.MSG_REPORT_TRACKS):
            return
        kind, idx = data[2], data[3]
        value = cb.decode_float(data[4:9])
        def apply():
            with self.lock:
                self._advance()
                if kind == cb.MSG_VALUE:
                    if idx == cb.DECK_PLAY_IDX[0]:
                        if self.playing != (value > 0.5):
                            self.playing = value > 0.5
                            self._play_changed(local=False)
                    else:
                        self.values[idx] = value
                elif kind == cb.MSG_SEEK and idx == 0:
                    self.pos = value
                    if not self.playing:
                        self._emit(cb.MSG_POSITION, 0, self.pos)
                elif kind == cb.MSG_TRIM and idx == 0:
                    self.trim = value
        threading.Timer(APPLY_DELAY, apply).start()


def make_bridge(sim, listen, peer, leader, impair, skew, drift=0.0):
    args = types.SimpleNamespace(
        leader=leader, verbose=False, test_clock_skew=skew, test_clock_drift=drift,
        clock_log=None, no_deck_sync=False, sync_log=None, impair=impair,
        peer=f"127.0.0.1:{peer}", listen=listen, virtual=False, midi="sim", own_decks=None, library=None)
    mido.get_input_names = lambda: ["sim"]
    mido.get_output_names = lambda: ["sim"]
    holder = {}
    mido.open_output = lambda name: FakePort(sim=sim)
    def open_input(name, callback=None):
        holder["port"] = FakePort(callback=callback)
        return holder["port"]
    mido.open_input = open_input
    b = cb.Bridge(args)
    sim.port_in = holder["port"]
    return b


def main(impair, title):
    random.seed(1)
    lead_sim, foll_sim = SimMixxx("leader", 0.0), SimMixxx("follower", -18.6)
    port = random.randint(20000, 40000)
    leader = make_bridge(lead_sim, port, port + 1, True, impair, 0.0)
    follower = make_bridge(foll_sim, port + 1, port, False, None, 123.0, 150.0)
    for b in (leader, follower):
        threading.Thread(target=b.recv_loop, daemon=True).start()
        threading.Thread(target=b.hello_loop, daemon=True).start()
        threading.Thread(target=b.clock_loop, daemon=True).start()

    log = []
    t0 = time.perf_counter()
    def at(t):
        while time.perf_counter() - t0 < t:
            err = (foll_sim.position() - lead_sim.position()) * TRACK * 1000
            log.append((time.perf_counter() - t0, err, lead_sim.playing, foll_sim.playing))
            time.sleep(0.01)

    at(1); foll_sim.press_play(False)       # follower has its own (old) pause on record
    at(4)                                   # connect, clock lock
    lead_sim.press_play(True); at(12)
    lead_sim.jump(30.0); at(20)               # hotcue press on the owner's deck
    for i in range(30):                     # wiggle a fader through the bad link
        lead_sim.press_value(2, i / 30); at(20 + 0.05 * (i + 1))
    lead_sim.press_value(2, 0.777); at(24)
    lead_sim.press_play(False); at(27)
    lead_sim.press_play(True); at(40)

    def span(a, b, playing_only=True):
        v = [e for t, e, lp, fp in log if a <= t < b and (lp and fp or not playing_only)]
        return (f"{min(v):+8.1f} .. {max(v):+8.1f} ms" if v else "n/a")
    print(f"=== {title} ===")
    print("  first 2 s after play     :", span(4, 6))
    print("  6-12 s, steady           :", span(6, 12))
    print("  12-13 s, owner jumped 30 s:", span(12, 13))
    print("  13-20 s, after the jump  :", span(13, 20))
    print("  20-24 s, during fader burst:", span(20, 24))
    print("  while both paused (24-27):", span(24.5, 27, playing_only=False))
    print("  2 s after second play    :", span(27, 29))
    print("  29-40 s, steady          :", span(29, 40))
    print("  follower volume:", foll_sim.values.get(2), "(leader 0.777)")
    s = follower.deck_sync[0]
    print("  seeks:", s.seeks, " leader reports:", s.leader_reports, " own:", s.own_reports)
    late = [t for t, e, lp, fp in log if lp != fp]
    print("  leader still playing at end:", lead_sim.playing)
    print("  time decks disagreed on play state: %.2f s" % (len(late) * 0.01))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "none" else None,
         sys.argv[2] if len(sys.argv) > 2 else "")
