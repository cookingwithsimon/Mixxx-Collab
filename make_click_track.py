"""Generate a click track for measuring sync between two machines.

    python make_click_track.py                      # click_120bpm_10min.wav
    python make_click_track.py --minutes 2 --bpm 100

A sharp 10 ms click on every beat, starting exactly at sample 0, with a
higher-pitched accent on the first beat of each bar. WAV is used on purpose:
lossy formats add decoder delay that can differ between machines.
"""

import argparse
import array
import math
import wave

RATE = 44100
CLICK_SECONDS = 0.010


def click(freq, amplitude):
    # Starts at full amplitude (cosine) so the onset is a single sharp edge.
    n = int(RATE * CLICK_SECONDS)
    return [int(32767 * amplitude * math.cos(2 * math.pi * freq * i / RATE) * math.exp(-i / (n / 5)))
            for i in range(n)]


def main():
    p = argparse.ArgumentParser(description="Generate a sync-test click track")
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--bpm", type=float, default=120)
    p.add_argument("--out", help="output file (default: click_<bpm>bpm_<minutes>min.wav)")
    args = p.parse_args()
    out = args.out or f"click_{args.bpm:g}bpm_{args.minutes:g}min.wav"

    total = int(RATE * 60 * args.minutes)
    samples = array.array("h", bytes(2 * total))
    beat, accent = click(1000, 0.7), click(2000, 0.9)
    beats = int(args.minutes * args.bpm)
    for b in range(beats):
        start = round(b * 60 / args.bpm * RATE)
        for i, s in enumerate(accent if b % 4 == 0 else beat):
            if start + i < total:
                samples[start + i] = s

    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    print(f"{out}: {beats} clicks at {args.bpm:g} BPM, {args.minutes:g} min, mono {RATE} Hz 16-bit")


if __name__ == "__main__":
    main()
