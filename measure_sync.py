"""Measure how far apart two machines play the click track (Windows).

Records the other machine through this PC's line-in and, at the same time,
this PC's own Mixxx output through WASAPI loopback, then compares when each
click lands in the two recordings.

    python measure_sync.py devices
    python measure_sync.py record --input "Line In" --loopback "Headphones" --minutes 10
    python measure_sync.py analyse sync_remote.wav sync_local.wav

The two recordings travel different paths with different, unknown delays, so
the absolute gap includes a constant that is NOT the machines' real offset.
Read the *change* in the gap over time (drift), not its level. For that to be
valid, Mixxx on this PC must play through the same sound card the line-in
belongs to, so both recordings share one sample clock.
"""

import argparse
import csv
import sys
import time
import wave

import numpy as np


# ---------------------------------------------------------------- recording

def wasapi_inputs(p, pa):
    api = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] == api and d["maxInputChannels"] > 0:
            yield d


def pick(devices, wanted, loopback):
    matches = [d for d in devices
               if bool(d.get("isLoopbackDevice")) == loopback and wanted.lower() in d["name"].lower()]
    if len(matches) != 1:
        kind = "loopback" if loopback else "input"
        names = [d["name"] for d in devices if bool(d.get("isLoopbackDevice")) == loopback]
        sys.exit(f"Need exactly one {kind} device matching '{wanted}', found {len(matches)}. Available: {names}")
    return matches[0]


class Recorder:
    def __init__(self, p, pa, dev, path):
        self.rate = int(dev["defaultSampleRate"])
        self.channels = dev["maxInputChannels"]
        self.frames = 0
        self.started = None  # perf_counter time of the first sample
        self.wav = wave.open(path, "wb")
        self.wav.setnchannels(self.channels)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)
        self.pa = pa
        self.stream = p.open(format=pa.paInt16, channels=self.channels, rate=self.rate,
                             input=True, input_device_index=dev["index"],
                             frames_per_buffer=1024, stream_callback=self.callback)

    def callback(self, data, frame_count, time_info, status):
        if self.started is None:
            self.started = time.perf_counter() - frame_count / self.rate
        self.wav.writeframes(data)
        self.frames += frame_count
        return (None, self.pa.paContinue)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.wav.close()


def cmd_devices(args):
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    for d in wasapi_inputs(p, pa):
        kind = "loopback" if d.get("isLoopbackDevice") else "input   "
        print(f"{kind}  {d['name']}  ({d['maxInputChannels']} ch, {int(d['defaultSampleRate'])} Hz)")
    p.terminate()


def cmd_record(args):
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    devices = list(wasapi_inputs(p, pa))
    remote_dev = pick(devices, args.input, loopback=False)
    local_dev = pick(devices, args.loopback, loopback=True)
    remote_path, local_path = f"{args.out}_remote.wav", f"{args.out}_local.wav"
    print(f"Remote (line-in):  {remote_dev['name']}")
    print(f"Local (loopback):  {local_dev['name']}")

    remote = Recorder(p, pa, remote_dev, remote_path)
    local = Recorder(p, pa, local_dev, local_path)
    seconds = args.minutes * 60
    start = time.perf_counter()
    try:
        while time.perf_counter() - start < seconds:
            time.sleep(1)
            left = seconds - (time.perf_counter() - start)
            print(f"\rRecording... {max(0, left):5.0f} s left", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped early")
    elapsed = time.perf_counter() - start
    remote.close()
    local.close()
    p.terminate()
    print()

    for name, r in (("remote", remote), ("local", local)):
        got = r.frames / r.rate
        if got < elapsed * 0.98:
            print(f"WARNING: {name} recording has {got:.1f} s of audio for {elapsed:.1f} s of time. "
                  "Loopback only delivers audio while something is playing to that device; "
                  "keep Mixxx running and outputting to it.")
    # How much later the local stream's first sample is than the remote one's.
    start_gap = (local.started or 0) - (remote.started or 0)
    analyse(remote_path, local_path, args.bpm, start_gap, args.out + ".csv")


# ----------------------------------------------------------------- analysis

def load(path):
    with wave.open(path, "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    return x.reshape(-1, channels).mean(axis=1), rate


def onsets(x, rate):
    """Return (times, is_accent) for each click onset."""
    level = np.abs(x)
    if level.size == 0 or level.max() < 50:
        return np.array([]), np.array([], dtype=bool)
    threshold = 0.3 * np.percentile(level, 99.99)
    above = np.flatnonzero(level > threshold)
    if above.size == 0:
        return np.array([]), np.array([], dtype=bool)
    first = np.r_[True, np.diff(above) > 0.1 * rate]  # new click after 100 ms of quiet
    idx = above[first]
    # Accents are 2 kHz, ordinary clicks 1 kHz: count zero crossings in 4 ms.
    n = int(0.004 * rate)
    accent = np.zeros(idx.size, dtype=bool)
    for k, i in enumerate(idx):
        seg = x[i:i + n]
        crossings = np.count_nonzero(np.diff(np.signbit(seg)))
        accent[k] = crossings / (2 * 0.004) > 1500
    return idx / rate, accent


def analyse(remote_path, local_path, bpm=120.0, start_gap=0.0, csv_path=None):
    remote, r_rate = load(remote_path)
    local, l_rate = load(local_path)
    rt, r_acc = onsets(remote, r_rate)
    lt, l_acc = onsets(local, l_rate)
    print(f"Clicks found: remote {rt.size}, local {lt.size}")
    if rt.size < 8 or lt.size < 8:
        print("Not enough clicks in one of the recordings. Check levels, the input device, "
              "and that both Mixxx instances were playing the click track.")
        return None

    # Match on accents when both have them (unambiguous to +/-1 s at 120 BPM),
    # otherwise on every click (+/- a quarter second).
    period = 60.0 / bpm
    if r_acc.sum() >= 4 and l_acc.sum() >= 4:
        rt, lt, window = rt[r_acc], lt[l_acc], 2 * period
        print("Matching on bar accents")
    else:
        window = period / 2
        print("Accents not detected; matching on every click")
    lt = lt + start_gap

    # For each remote click, the nearest local click. gap > 0: remote is later.
    pos = np.clip(np.searchsorted(lt, rt), 1, lt.size - 1)
    nearest = np.where(np.abs(lt[pos] - rt) < np.abs(lt[pos - 1] - rt), lt[pos], lt[pos - 1])
    gap = rt - nearest
    keep = np.abs(gap) < window
    t, gap_ms = rt[keep], gap[keep] * 1000
    if t.size < 8:
        print("Could not match clicks between the recordings.")
        return None

    slope, intercept = np.polyfit(t, gap_ms, 1)          # ms per second
    residual = gap_ms - (slope * t + intercept)
    print(f"\nMatched {t.size} clicks over {t[-1] - t[0]:.0f} s")
    print(f"{'time':>8}  {'gap (ms)':>9}  {'change (ms)':>11}")
    first_gap = np.median(gap_ms[t < t[0] + 10])
    for start in np.arange(t[0], t[-1], 30.0):
        chunk = gap_ms[(t >= start) & (t < start + 30)]
        if chunk.size:
            g = np.median(chunk)
            print(f"{start - t[0]:7.0f}s  {g:+9.2f}  {g - first_gap:+11.2f}")
    total = slope * (t[-1] - t[0])
    print(f"\nDrift: {total:+.2f} ms over {t[-1] - t[0]:.0f} s  ({slope * 1000:+.1f} ppm)")
    print(f"Range of gap: {gap_ms.max() - gap_ms.min():.2f} ms   "
          f"Wobble around the trend: {residual.std():.2f} ms RMS")
    print("The gap's level includes an unknown constant from the two recording paths; "
          "only its change is meaningful.")

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            out = csv.writer(f)
            out.writerow(["time_s", "gap_ms"])
            out.writerows(zip(np.round(t, 4), np.round(gap_ms, 3)))
        print(f"Per-click data written to {csv_path}")
    return slope, gap_ms


def main():
    p = argparse.ArgumentParser(description="Measure click-track sync between two machines")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list capture devices")
    r = sub.add_parser("record", help="record both machines, then analyse")
    r.add_argument("--input", required=True, help="line-in device name (substring)")
    r.add_argument("--loopback", required=True, help="output device Mixxx plays to (substring)")
    r.add_argument("--minutes", type=float, default=10)
    r.add_argument("--bpm", type=float, default=120)
    r.add_argument("--out", default="sync", help="output file prefix")
    a = sub.add_parser("analyse", help="analyse two existing recordings")
    a.add_argument("remote")
    a.add_argument("local")
    a.add_argument("--bpm", type=float, default=120)
    a.add_argument("--csv")
    args = p.parse_args()

    if args.cmd == "devices":
        cmd_devices(args)
    elif args.cmd == "record":
        cmd_record(args)
    else:
        analyse(args.remote, args.local, args.bpm, 0.0, args.csv)


if __name__ == "__main__":
    main()
