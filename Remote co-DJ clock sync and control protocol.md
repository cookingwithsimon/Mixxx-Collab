# Remote co-DJ: clock sync and control protocol

Sep 30, 2026 · @Simon

Two DJs run Mixxx with identical tracks; the leader owns a session clock, and every control change travels as a timestamped event that both machines apply at the same session time.

## Design goals

The protocol syncs control state and a shared clock, never audio, so each DJ hears their own actions with no network delay and the partner's actions a fixed, small time later.

- **What travels:** deck and mixer control changes (play, cue, rate, EQ, faders, effects, loops) and the clock-sync packets.
- **What stays local:** audio rendering, waveforms, the library database, controller LEDs and screens.
- **Clock target:** under 5 ms of clock error between machines on ordinary home broadband. This is a design target to test, not a measured result.
- **Roles:** one machine is the leader and owns the session clock; the other is the follower. The leader is chosen at session start and can be handed over.
- **Deck ownership (assumed):** each DJ controls two decks, with the split chosen at the start of each session (for example the leader Decks 1 and 2, the follower Decks 3 and 4), and the crossfader and master gain are held by one DJ at a time through a control token.
- **Transport:** an unordered, unreliable datagram channel (a WebRTC data channel; see the integration plan) for clock packets and continuous controls, plus a reliable ordered channel for discrete commands.
- **Precondition:** both machines hold byte-identical audio files, kept in sync by a shared folder and verified by hash before a track can be loaded (see the Mixxx integration plan).

Scratching is out of scope: the protocol targets basic mixing with play, cue, tempo, EQ, faders and effects.

## Clock sync

The follower estimates its offset from the leader's session clock with repeated ping-pong exchanges and trusts the samples with the shortest round trip.

1. The follower sends `PING` stamped with its local time t0.
2. The leader receives it at t1 (session clock) and replies `PONG` carrying t0, t1 and its send time t2.
3. The follower receives the reply at t3.
4. The follower computes round trip and offset from the four stamps.

```latex
\text{RTT} = (t_3 - t_0) - (t_2 - t_1) \qquad \text{offset} = \frac{(t_1 - t_0) + (t_2 - t_3)}{2}
```

Offset is the amount to add to the follower's clock to get session time. The values below are starting points to tune in testing.

- **Join burst:** 8 pings, 50 ms apart. **Steady state:** one ping every 2 s.
- **Filtering:** keep the last 32 samples, take the quarter with the lowest RTT, use the median offset of those.
- **Error bound:** the estimate assumes equal delay each way. If outbound is 30 ms and return is 20 ms, the offset is wrong by 5 ms, and one side cannot measure this. It is the main source of residual error.
- **Slew, don't step:** while any deck is playing, correct the follower clock by at most ±500 ppm (0.5 ms per second). Step it only when the offset exceeds 50 ms and nothing is playing.
- **Audio clock mapping:** session time must be mapped onto the sound card's buffer timeline, not just the system clock. The open Ableton Link pull request for Mixxx does this with a host-time filter that gives a jitter-free timestamp per audio buffer, and adds a manual latency setting because PortAudio's latency reports are unreliable on some audio APIs ([Mixxx PR #10999](https://github.com/mixxxdj/mixxx/pull/10999)).

## Shared timeline

Each deck's playhead is a function of session time, so both machines compute the same position without streaming positions back and forth.

```latex
\text{position}(t) = \text{anchorPos} + \text{rate} \times (t - \text{anchorTime}) \quad \text{while playing}
```

While paused, position stays at anchorPos. Every deck carries one anchor record:

| Field | Meaning |
| --- | --- |
| deck | Mixxx channel group, for example `[Channel1]` |
| trackHash | Hash of the loaded audio file |
| anchorTime | Session time of the last state change |
| anchorPos | Playhead position in sample frames at anchorTime |
| rate | Playback rate, 1.0 = track speed, includes the pitch fader |
| playing | True or false |
| loop | Start, end and enabled flag |

Play, pause, cue jump, seek, rate change and loop change each create a new anchor. Each event carries the whole new anchor, so a repeated or late event does the same thing as the first copy. Beat phase comes from the shared beatgrid for that trackHash, so two machines never disagree about where the beats are.

## Control events

Every control change becomes one small event that the sender applies immediately and the receiver applies at a session time slightly in the future, so both mixes converge on the same state.

```json
{
  "v": 1,
  "seq": 4182,
  "sender": "follower",
  "sendTime": 812340.250,
  "applyTime": 812340.310,
  "class": "command",
  "group": "[Channel3]",
  "key": "play",
  "value": 1,
  "anchor": { "anchorTime": 812340.250, "anchorPos": 1440000, "rate": 1.0 }
}
```

Times are seconds on the session clock. JSON is for development; a compact binary encoding can follow once the fields settle.

| Field | Meaning |
| --- | --- |
| v | Protocol version |
| seq | Per-sender counter, used to detect loss and reordering |
| sender | `leader` or `follower` |
| sendTime | Session time when the sender made the change |
| applyTime | Session time the receiver should apply it: sendTime plus the margin |
| class | `command`, `state` or `stream` |
| group, key | The Mixxx control, as a group and item name |
| value | The new value |
| anchor | Deck anchor record, present on transport events only |

### Three event classes

| Class | Examples | Channel | Rule |
| --- | --- | --- | --- |
| command | play, cue, load, loop, sync toggle | Reliable, ordered | Never dropped; applied in seq order |
| state | EQ, gain, crossfader, effect parameters | Datagram | Newest seq wins; last value resent every 500 ms so a lost packet self-heals |
| stream | Pitch bend, filter sweeps | Datagram | Sent at up to 60 Hz; late samples are dropped, not replayed |

### Apply time

- For deck controls, the sender applies its own change immediately, so its DJ hears no delay. Shared mixer controls follow the lockstep setting described under Control ownership and handoff.
- The receiver applies at the later of arrival time and applyTime.
- The margin is the one-way delay estimate plus 3 standard deviations of recent jitter, floored at 40 ms and capped at 250 ms. These are starting values to tune.
- Transport events carry anchorTime equal to sendTime. A receiver that applies late computes the current position from the anchor and joins the deck at the right place, so the listener may hear the first part of a start clipped but never misaligned.

## Control ownership and handoff

Every control has one writer at a time, so nothing needs conflict resolution and nobody's own controls wait for the network.

| Controls | Writer | Behaviour |
| --- | --- | --- |
| Deck transport, channel fader, EQ, gain, effects, loops | The DJ who owns the deck | Applied immediately on the owner's machine, mirrored on the partner's after the margin |
| Crossfader and master gain | The holder of the control token | Same, or lockstep if enabled; the other DJ's physical controls for these are ignored |

### The control token

- The leader starts with the token, and a visible indicator on both screens shows who holds it.
- **Hand over:** the holder presses a button and sends `HANDOVER`; the token moves when the partner acknowledges.
- **Take control:** the other DJ presses a button and sends `TAKE`. The leader arbitrates, so simultaneous presses have one winner, and grants it immediately.
- **Epoch:** every transfer increments a token epoch. Shared-mixer events from an older epoch are dropped, so a late move from the previous holder can never fight the new one.
- **No jump on takeover:** the new holder's physical crossfader may sit elsewhere, so soft takeover, which Mixxx mappings can enable, holds the value until the fader passes it.
- Both actions are buttons on the session panel (see the integration plan) and can be mapped to a spare controller button.

### Lockstep for the shared mixer

Lockstep is off by default and stays off until testing shows the delay is unnoticeable. When on, it makes both machines apply a crossfader or master-gain change at the same session time, so both mixes match exactly during a fader move. It delays only the token holder's shared controls, by the margin (at least 40 ms, adapting to the connection; 40 ms may be acceptable, but testing decides); deck controls stay immediate in every mode. With scratching out of scope, I expect a delay of that size on a fader move to be hard to notice in a normal blend, but that needs testing. The default instant mode skips the delay and lets the partner's machine catch up after the margin instead.

## Late events and reconciliation

A late event is applied on arrival and any position error is corrected by a gentle speed nudge; audio only jumps when the error is large.

| Situation | Response |
| --- | --- |
| Command arrives after its applyTime | Apply now; transport events land at the right position via the anchor |
| State event arrives after a newer one | Discard it (lower seq) |
| Stream sample more than 100 ms old | Drop it |
| Duplicate seq | Ignore |

The owner of a deck is authoritative for its anchor. Each side sends a heartbeat once per second listing the anchor of every deck it owns, and the other side compares the position it computes against its own playhead:

| Position error | Action |
| --- | --- |
| Under 5 ms | Nothing |
| 5 to 50 ms | Nudge playback rate by up to ±0.5% until the error closes; keylock holds pitch steady |
| Over 50 ms | Seek to the computed position |

These thresholds are starting values. Heartbeats are also what repair a deck after packet loss, a stalled audio thread or a machine that briefly slept.

## Connection lifecycle

A session moves from connect to clock lock to library check to live, and a dropped link re-enters at clock lock with a full state snapshot.

1. **Connect:** the leader's app first tries to open its signalling port with UPnP. If that works, the follower connects straight to it; if UPnP fails or the follower cannot reach the port, the app falls back to an invite code the leader sends by chat and the follower answers with a reply code. Either way, the peers then exchange a session ID, roles, the deck split and protocol version, and open a control connection with a reliable and a datagram channel, plus a separate file connection, through NAT traversal. Pairing also creates a per-session secret, saved on both machines for reconnects.
2. **Clock lock:** run the join burst; the session is clock-locked when the last 8 offset samples agree within 2 ms.
3. **Library check:** the two machines exchange manifests of the shared folder and missing files copy across in the background. A track loads on a shared deck only when both hashes match; otherwise that deck shows "missing on partner".
4. **Live:** events flow, heartbeats run once per second, and the control token starts with the leader.
5. **Drop:** after 3 s with no packet the partner is marked offline. Decks keep playing from their anchors, because position derives from time, but the partner's controls stop.
6. **Reconnect without a code:** both sides retry automatically, with increasing gaps between attempts, in this order: an ICE restart over the old path if the connection is only stalled; the leader's UPnP-mapped port, if one exists; then simultaneous sends to each other's last known public addresses, which often reopen the NAT path. Every attempt is authenticated with the saved session secret. Only when all of these fail, typically because a public address changed and no UPnP port exists, does the app ask for a new invite code.
7. **Resume:** once reconnected, redo clock lock, then each side sends a snapshot of its deck anchors, all mixer state and the token holder and epoch. The receiver applies it, and seq counters continue from the last value seen.

**Leader handover:** the old leader sends `HANDOVER` naming the current session time. The new leader adopts that time as its own clock without a jump, and the old leader becomes a follower and runs the handshake. If the leader is offline for over 10 s, the follower may promote itself from its last offset estimate.

## Open questions

- [ ] Lockstep: is a delay of 40 ms or more on fader moves unnoticeable? If so, it can become the default.
- [ ] Packet priority: does DSCP marking of the control connection change jitter or round-trip time?
- [ ] Reachability: how often does UPnP fail, so that the invite code is needed?
