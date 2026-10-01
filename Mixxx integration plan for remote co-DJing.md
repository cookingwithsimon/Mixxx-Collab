# Mixxx integration plan for remote co-DJing

Sep 30, 2026 · @Simon

Build the remote-DJ layer as a networking module that replicates Mixxx control changes and drives its internal sync clock, so Mixxx's audio engine stays untouched.

## Approach

Mixxx already exposes what a remote layer needs: a named control for every deck and mixer parameter, a sync engine with a leader and an internal clock, and a scripting layer for controllers. The plan is a networking module that reads and writes those controls and drives the sync clock, with as little change to Mixxx's core as possible. The wire format and clock maths are in the [protocol spec](https://claude.ai/code/artifact/16fda9e9-be7d-4ea6-89bf-b5417093cc80).

- **Keep the fork thin.** Changes that could be offered upstream (a pluggable clock source, a hook for observing control changes) stay separate from the product code.
- **Don't depend on Ableton Link.** As far as I could confirm, Link support is a long-running pull request rather than part of a Mixxx release, and Link is designed for local networks. Its timing lessons still apply (see Risks).
- **Start with two people and four decks.** Each DJ owns two decks; the crossfader and master gain pass between them with a control token.

## Hook points in Mixxx

Five existing pieces of Mixxx cover the module's needs; none has to be rewritten.

| Area | What Mixxx provides | How the module uses it |
| --- | --- | --- |
| Controls | Every deck and mixer parameter is a named control, addressed by group and item, for example `[Channel1]` `play`. Scripts read and write them with `engine.getValue` and `engine.setValue`, and get change callbacks from `engine.makeConnection` ([scripting wiki](https://github.com/mixxxdj/mixxx/wiki/Midi-Scripting)). | Observe changes on the local decks and mixer and publish them as events; apply incoming events by setting the same controls. |
| Sync engine | Sync Lock with a sync leader and an internal clock, in `src/engine/sync/`. Followers adjust their speed to stay in step with the leader ([PR #11035](https://github.com/mixxxdj/mixxx/pull/11035)). | Drive the internal clock from the session clock, so a sync-locked deck on either machine follows the shared tempo and phase. |
| Audio callback | Buffers are produced on the sound card's callback through PortAudio; output latency reports vary by audio API ([PR #10999](https://github.com/mixxxdj/mixxx/pull/10999)). | Map session time to each buffer's start time, with a manual latency setting as the fallback. |
| Controller mappings | Per-controller JavaScript and XML mappings, with a growing library maintained by the community. | Leave untouched. The controller drives local controls; the module sees only the resulting control changes. |
| Library | Track database with BPM, beatgrids and cue points. | Add a file hash per track and a shared analysis record (see Shared folder and analysis sync). |

## Module structure

&#91;embedded content: module structure · 3 services, 4 Mixxx hooks\]

Three services share one transport: the event bridge uses Mixxx's controls, the clock service drives the sync engine and audio timing, and library sync watches the shared folder and reads and writes the track database. The controller mapping is untouched.

## Transport choice

Use WebRTC data channels: they give low latency, already solve NAT traversal, and work with the widest range of software, at the cost of running a small signalling service.

| Option | Latency | Interoperability | Getting through home routers | Effort |
| --- | --- | --- | --- | --- |
| WebRTC data channels | Low; unordered, no-retransmit channels suit clock and state packets, and a reliable channel carries commands | Widest: a standard supported by browsers and several native libraries | Built in (ICE with STUN, with a relay fallback) | Moderate; needs a small signalling server |
| QUIC datagrams | Low | Newer, with fewer libraries for peer-to-peer use | Not built in; you add hole punching | Higher |
| Custom UDP | Lowest overhead | None; it is your own protocol | Not built in; you write STUN handling and hole punching | Highest, since encryption and reliability are also yours |

The QUIC and custom UDP rows are my own assessment; the WebRTC row rests on the library described below.

- **Library:** [libdatachannel](https://github.com/paullouisageneau/libdatachannel) is a standalone C++ implementation of WebRTC data channels for Windows, macOS and Linux. It is compatible with browsers and other WebRTC libraries, uses libjuice for connectivity and WebSocket for signalling. It has been MPL 2.0 licensed since version 0.18 (earlier versions were LGPLv2.1 or later), so check the licence against your distribution plans.
- **Channels:** one reliable ordered channel for commands, token changes and analysis records; one unordered channel with no retransmits for clock packets, state and heartbeats; and a separate second connection for file transfer. That keeps a large file from sharing congestion control or packet markings with the clock packets, though it can still fill the home router's queue, so the transfer throttle still matters.
- **Signalling:** the leader's app hosts a small server that exchanges connection details. It carries no audio or files. The leader's app opens its port with UPnP where the router allows it (see Reachability and permissions); where that fails, the fallback is an invite code the leader sends by chat and the follower answers with a reply code, which needs no server at all. A STUN server is still needed to discover public addresses; public ones exist, or you can run your own.
- **Relay fallback:** when no direct path exists, traffic goes through a relay, which adds latency and bandwidth cost. Measure how often that happens before relying on it.

### Packet priority (DSCP)

Marking the control connection's packets as higher priority is worth trying as an optional setting, but expect a small benefit. The W3C specification says marking can help in some environments, notably wireless, and it names bleaching (clearing or ignoring markings) as the simplest network configuration, so many networks will ignore it ([W3C](https://www.w3.org/TR/webrtc-dscp/)). The likeliest gain is on the local Wi-Fi hop and the home router's own queue.

- **One marking per connection.** According to the IETF WebRTC QoS draft, all data channel traffic on one connection carries a single marking ([IETF draft](https://www.ietf.org/archive/id/draft-ietf-tsvwg-rtcweb-qos-18.xml)). Clock packets can only be prioritised over file transfer if they use separate connections, as planned above.
- **Windows is awkward.** Windows silently ignores a marking set directly on a socket ([PJSIP QoS notes](https://docs.pjsip.org/en/latest/api/generated/pjlib/group/group__socket__qos.html)). An app has to use the QoS2 (qWAVE) API, which may need administrator rights, or the user needs a Group Policy QoS policy. Start with Linux and macOS, and treat Windows as best effort.
- **Library support unknown.** I could not confirm that libdatachannel exposes a DSCP setting, so check its API or set the option on the underlying socket.
- **Starting value.** EF (46), the usual real-time voice class; the IETF draft defines its own defaults, so check them.
- **Decide by measuring.** Keep it behind a setting. The app can also measure live: send a few clock pings over the unmarked file connection too, compare their jitter with the marked control connection, and turn marking off for that session if the marked path is worse.

#### Will marking increase jitter?

The evidence suggests marking usually does nothing, sometimes helps a little, and rarely makes things worse; no study I found measured it increasing jitter directly.

| Where | What the evidence says | Effect on jitter |
| --- | --- | --- |
| Your Wi-Fi hop | Access points map DSCP to four Wi-Fi priority queues. The voice queue waits a shorter time and uses a smaller contention window before sending ([802.11e](https://en.wikipedia.org/wiki/IEEE_802.11e-2005)). Many vendors map by the top three bits, so EF lands in the video queue rather than voice ([RFC 8325 summary](https://mrncciew.com/2021/09/14/rfc-8325-wifi-qos-mappings/)) | Likely lower when the Wi-Fi is busy; the likeliest real gain |
| Internet core | Across more than 100,000 links, routers on about 3% treated the WebRTC marks differently and consistently cut delay under congestion ([Barik et al.](https://www.researchgate.net/publication/335417482_On_the_utility_of_unregulated_IP_DiffServ_Code_Point_DSCP_usage_by_end_systems)) | Small help on a few links |
| Mobile networks | Marks have a 47% to 100% chance of being replaced within the first two hops, and one operator remarked a lower class to a higher one and vice versa ([Custura et al.](https://tma.ifip.org/wp-content/uploads/sites/7/2017/06/mnm2017_paper13.pdf)) | Usually none; wrong treatment possible |
| Managed networks | Some providers deliberately remark customer EF to CS1, a low-priority class, to stop customers claiming priority ([Cisco Community](https://community.cisco.com/t5/switching/dscp-value/td-p/2394466)) | Could be worse under congestion |

The one concrete way marking could raise jitter is remarking into a low-priority class on a congested link. That is rare on home broadband but is why the live comparison above is worth building.

### Reachability and permissions

UPnP is a reasonable way to make the leader reachable, provided it is treated as best effort with fallbacks. These points come from general networking practice rather than a checked source.

- **Map only what is needed, only while needed.** Request a temporary mapping for the signalling port, and optionally a fixed UDP port for connection setup if the library lets you pin its port range. Give the mapping a lease that expires, and remove it when the session ends. A UPnP library such as miniupnpc can do this; check its licence.
- **Expect it to fail sometimes.** UPnP is often switched off on routers, and it cannot help when the ISP puts the connection behind carrier-grade NAT (one public address shared between customers). Keep the STUN connection setup and the invite-code fallback, and show a clear message when the mapping fails.
- **Authenticate the open port.** Anyone on the internet can reach a mapped port, so the first message must carry a per-session secret from the invite, and the port closes once pairing is done.
- **Admin rights are not needed for UPnP itself.** They are useful on Windows for two one-time steps: the firewall rule and a Policy-based QoS rule that marks the program's packets. Doing those from an elevated installer or helper is better than running the whole program elevated, because an elevated Windows app usually does not accept drag and drop from a normal Explorer window, which would break dropping tracks into the shared folder. Running elevated still works if you accept that limit.

## Shared folder and analysis sync

Each DJ keeps a shared music folder that mirrors directly between the two machines, and the leader's beatgrid analysis travels with each file's hash so both decks agree on where the beats are.

1. **Folder.** Each machine registers a "Shared music" folder as a Mixxx library directory. Dropping a file into it, or using an upload button in the app, adds it to the share.
2. **Manifest.** Each side keeps a manifest of relative path, size and SHA-256 hash. On connect the two exchange manifests and request whatever the other lacks. A hash of the file bytes is used because an audio fingerprint identifies the same song but not the same encode.
3. **Transfer.** Files move in chunks over their own connection directly between the two machines, resume after a drop, and are checked against the hash before they are moved into place and added to the library.
4. **Analysis by hash.** For each hash, keep one record of BPM, beatgrid and cue points. The leader's record wins, is sent over the reliable channel at join and on every change, and is stored on the follower as an override.
5. **Folder rules.** Deletions do not propagate; a removed file is only flagged. Two different files with the same name are both kept, with the hash added to one name. A track outside the shared folder can load on a local deck, marked "not shared", but never on a shared one.
6. **Throttle while live.** A bulk upload can fill a home uplink and raise round-trip time, which hurts clock sync. Cap transfers to a fraction of the measured upload speed during a live session (50% as a starting value); the clock filter already ignores high-RTT samples. For scale, at 5 Mbit/s uncapped, an 8 MB MP3 takes about 13 s and a 30 MB FLAC about 48 s.

This keeps the music between the two machines, so syncing only works while both are online. An optional bring-your-own-storage backend, such as the users' own cloud bucket or SFTP server, could later allow syncing when only one is online. If the app instead stored files on a server you run, you would be hosting users' uploads, which carries different legal and cost considerations from a tool that never stores them. I'm not a lawyer, and I would get advice before choosing that route.

Users are responsible for holding the rights to the music they share.

## Controllers

The controller stays a local device that moves local controls, and the module replicates the resulting control changes, so in principle any controller Mixxx already has a mapping for works without new mapping work.

| Input | Behaviour |
| --- | --- |
| Buttons, faders, knobs | Become control changes and travel as commands or state events; the crossfader and master gain are accepted only from the DJ holding the control token |
| Jog wheel on your own deck | Nudging runs on local audio and reaches the partner through anchor changes and heartbeats; scratching is out of scope |
| Jog wheel on the partner's deck | Not offered in the first version, because a tight feedback loop over the internet feels poor |
| LEDs, meters, screens | Driven by local Mixxx state, so they also reflect replicated changes from the partner |
| Controllers with vendor or HID protocols | Work if Mixxx already has a mapping for them; the mapping runs locally |

Testing should start with one common MIDI controller on each side, then add a second model to check that nothing in the module assumes a particular mapping.

## Session panel

A small, always-visible panel shows the state of the session and holds the handoff buttons, so neither DJ needs a terminal or settings screen during a set.

### Indicators

| Indicator | Shows |
| --- | --- |
| Partner | Name, and online, reconnecting or offline; offline shows how long ago the last packet arrived |
| Link | Round trip in ms and a quality light: green under 30 ms with no recent loss, amber above that or with recent stalls, red when offline |
| Clock | Locked or syncing; while syncing, the partner's changes still apply but deck sync waits |
| Role | Whether this machine is the session leader |
| Crossfader and master | Who holds the control token: "You" or the partner's name |
| Each deck | Owner ("Yours" or the partner's name), and sync state: in sync, correcting, paused, different track, or missing on this machine |

### Buttons

| Button | Shown when | Effect |
| --- | --- | --- |
| Hand over | You hold the token | Sends `HANDOVER`; the button shows "Waiting…" until the partner acknowledges, then the token indicator changes on both screens |
| Take control | The partner holds the token | Sends `TAKE`; the leader arbitrates, so if both press at once one wins and the other sees "Partner kept control" |
| Give deck / Take deck | Per deck, in a small menu | Moves deck ownership, and with it whose playhead is authoritative; asks the other DJ to confirm, because their deck may jump |
| Make me leader | Behind a confirmation | Leader handover, for when the leader's machine is about to leave the session |

Every handoff also appears on the other DJ's panel as a short notice, for example "Partner took the crossfader", so a change is never silent. After taking the token, soft takeover holds the crossfader until the new holder's physical fader passes its current position, as described in the protocol spec.

### Where it lives

| Option | For | Against |
| --- | --- | --- |
| A panel served by the companion app (a small local web page or native window) | Works with stock Mixxx and every skin; no change to the fork; fastest to build | A separate window to place next to Mixxx |
| Widgets in Mixxx's skins | Sits inside Mixxx | Needs new controls in the fork and edits to each skin, which makes the fork thicker |
| Controller buttons and LEDs | Hands stay on the hardware | Specific to each mapping; only controllers with spare buttons |

Start with the companion app's panel. Where a controller mapping has a spare button and LED, mirror "Take control" and the token indicator onto it, so the most common handoff doesn't need the screen.

## Milestones

Six stages, each ending in a check that can be run, ordered so the riskiest unknown (timing over the internet) is tested before the polish.

| Stage | Goal | Done when |
| --- | --- | --- |
| M0 Build | Build Mixxx from source on both machines | Both machines run it and play the same track |
| M1 Control replication on a LAN | Two instances mirror deck and mixer controls through the reliable channel | Play, cue and EQ on one machine change the other |
| M2 Shared clock | Ping-pong sync and a networked internal clock driving Sync Lock | Decks on two machines stay within 5 ms over 10 minutes, measured by recording both outputs playing a click track |
| M3 Internet play | NAT traversal with UPnP and the invite-code fallback, jitter margin and heartbeats | A 30-minute session across two home connections with no audible drift or dropouts |
| M4 Files and analysis | Shared-folder sync and shared beatgrids | A file dropped into the folder on one machine arrives on the other, hash-verified, and both sides show identical beat positions |
| M5 Controllers and resilience | Two controller models, control token handoff, leader handover, reconnect and the session panel | A session survives a 10-second network drop and resumes without a restart, and the token changes hands from the panel on either machine |

## Licensing and risks

The main constraints are Mixxx's GPL, the unmerged Link work, and sync-lock edge cases that a networked clock will inherit.

- **GPLv2.** Mixxx is released under the GPLv2, so a distributed derivative must also be offered under the GPL with its source. I'm not a lawyer; get advice before planning a closed or paid product.
- **Link is a reference, not a dependency.** Testers of the open Link pull request reported tempo jumps and exit crashes in earlier builds, and its author notes that latency reporting is unreliable on some audio APIs.
- **Known sync-lock edge cases.** Maintainers have documented problems with a stopped leader, non-constant beatgrids and scratching the leader. The networked clock needs tests for each.
- **The audio thread must never wait on the network.** Pass events to and from the audio callback through lock-free queues.
- **Version drift.** Pin one Mixxx version for the prototype; rebasing a fork onto a moving main branch is a recurring cost.

Sources (only PR #10999 was read in full; the rest were read as search excerpts):

- [Mixxx MIDI scripting wiki](https://github.com/mixxxdj/mixxx/wiki/Midi-Scripting)
- [Full Ableton Link support, PR #10999](https://github.com/mixxxdj/mixxx/pull/10999)
- [Sync Lock: end of track checking, PR #11035](https://github.com/mixxxdj/mixxx/pull/11035)
- [Sync Lock: protect against syncing phase to a stopped leader, PR #12388](https://github.com/mixxxdj/mixxx/pull/12388)
- [Auto DJ, sync and non-constant beatgrids, issue #9795](https://github.com/mixxxdj/mixxx/issues/9795)
- [Mixxx Ableton Link fork README, confirming GPLv2](https://github.com/bencejuhaasz/mixxx_ablink)
