// MixxxCollab bridge mapping — proof of concept
//
// Streams local control changes out as SysEx to the companion app, and applies
// changes coming back from the companion app (i.e. from the remote peer).
//
// SysEx format (all bytes after F0 must be < 0x80):
//   F0 7D <dir> <type> [<idx> <v0> <v1> <v2> <v3> <v4>] F7
//   dir:  0x01 = from Mixxx, 0x02 = to Mixxx
//   type: MSG_* below (control value, snapshot request, deck position/speed/loop,
//         and seek/trim from the bridge)
//   idx:  index into MixxxCollab.controls (must match collab_bridge.py)
//   v0-v4: float32 (big-endian bits) packed into 7-bit bytes
//
// Both Mixxx and the bridge share one loopMIDI port, so every message is seen by
// both sides. The <dir> byte lets each side ignore its own messages.

var MixxxCollab = {};

MixxxCollab.SYSEX_ID = 0x7D;          // non-commercial / educational ID
MixxxCollab.FROM_MIXXX = 0x01;
MixxxCollab.TO_MIXXX = 0x02;
MixxxCollab.MSG_VALUE = 0x01;
MixxxCollab.MSG_SNAPSHOT_REQUEST = 0x02;
MixxxCollab.MSG_POSITION = 0x03;  // from Mixxx: idx = deck, value = playposition
MixxxCollab.MSG_SEEK = 0x04;      // to Mixxx: idx = deck, value = playposition
MixxxCollab.MSG_TRIM = 0x05;      // to Mixxx: idx = deck, value = relative speed trim
MixxxCollab.MSG_SPEED = 0x06;     // from Mixxx: idx = deck, value = track fractions per second
MixxxCollab.MSG_LOOP = 0x07;      // from Mixxx: idx = deck, value = loop length as a track fraction
// Track paths, in chunks: <type> <deck> <chunk> <count> <nibbles...>, each
// UTF-8 byte as two 4-bit nibbles. Needs the MixxxCollab build of Mixxx,
// which adds engine.getTrackLocation() and engine.loadTrackFromLocation().
MixxxCollab.MSG_LOADED = 0x08;    // from Mixxx: the file now loaded on a deck
MixxxCollab.MSG_LOAD = 0x09;      // to Mixxx: load this file on a deck
MixxxCollab.MSG_REPORT_TRACKS = 0x0A;  // to Mixxx: report every deck's file again
MixxxCollab.PATH_CHUNK = 96;      // path bytes per SysEx message (loopMIDI caps SysEx at 256 bytes)
MixxxCollab.EPSILON = 1e-4;

// Deck sync: each deck's playposition is reported, and for decks the other
// machine owns, the bridge answers with seeks and small speed trims to stay
// on the owner's playhead. Keep in sync with DECKS in collab_bridge.py.
MixxxCollab.decks = ["[Channel1]", "[Channel2]", "[Channel3]", "[Channel4]"];
MixxxCollab.POSITION_INTERVAL_MS = 200;
MixxxCollab.JUMP_SECONDS = 0.1;   // a position change this far off the expected one is a jump
MixxxCollab.TRIM_ACTIVE_MS = 3000;
MixxxCollab.lastPositionSent = [];
MixxxCollab.trimRatio = [];       // speed factor currently applied on top of the rate
MixxxCollab.lastTrim = [];        // when the bridge last trimmed each deck
MixxxCollab.quantizeTimer = [];   // pending "turn quantize back on" timers
MixxxCollab.lastLocation = [];    // file last reported as loaded, per deck
MixxxCollab.loadChunks = [];      // path chunks arriving from the bridge, per deck
for (var d = 0; d < MixxxCollab.decks.length; d++) {
    MixxxCollab.lastLocation.push(null);
    MixxxCollab.loadChunks.push({});
    MixxxCollab.lastPositionSent.push(0);
    MixxxCollab.trimRatio.push(1);
    MixxxCollab.lastTrim.push(0);
    MixxxCollab.quantizeTimer.push(0);
}

// ORDER MATTERS — the index is what goes over the wire, and must stay under
// 128. Built the same way as CONTROLS in collab_bridge.py; change both.
// Which effect or chain preset is loaded travels as an index into Mixxx's
// effect lists, so it matches only if both machines list the same effects in
// the same order (true for the same Mixxx version with default settings).
// Loads come before their parameters, so a snapshot applies them first.
// Not synced on purpose: headphone cue (pfl), master/headphone gain, quantize
// and sync lock (the leader's rate is what travels), and momentary buttons
// such as cue, hotcues and beatjump, whose effect arrives as a position.
MixxxCollab.controls = [["[Master]", "crossfader"]];
MixxxCollab.decks.forEach(function(g) {
    MixxxCollab.controls.push(
        [g, "play"], [g, "volume"], [g, "pregain"], [g, "rate"], [g, "keylock"],
        ["[EqualizerRack1_" + g + "_Effect1]", "parameter1"],  // low
        ["[EqualizerRack1_" + g + "_Effect1]", "parameter2"],  // mid
        ["[EqualizerRack1_" + g + "_Effect1]", "parameter3"],  // high
        ["[QuickEffectRack1_" + g + "]", "loaded_chain_preset"],  // which quick effect
        ["[QuickEffectRack1_" + g + "]", "super1"],             // filter
        ["[QuickEffectRack1_" + g + "]", "enabled"],
        [g, "loop_start_position"], [g, "loop_end_position"], [g, "loop_enabled"]);
});
[1, 2].forEach(function(u) {
    var unit = "[EffectRack1_EffectUnit" + u + "]";
    MixxxCollab.controls.push([unit, "loaded_chain_preset"],
        [unit, "mix"], [unit, "super1"], [unit, "enabled"]);
    MixxxCollab.decks.forEach(function(g) {
        MixxxCollab.controls.push([unit, "group_" + g + "_enable"]);
    });
    [1, 2, 3].forEach(function(e) {
        var effect = "[EffectRack1_EffectUnit" + u + "_Effect" + e + "]";
        MixxxCollab.controls.push([effect, "loaded_effect"], [effect, "enabled"], [effect, "meta"]);
    });
});

// Values recently applied from the network, per control index, as [value, time]
// pairs, so the resulting change callbacks aren't echoed straight back to the
// peer. Callbacks arrive asynchronously and can lag behind a fast stream of
// remote values, so a single "last value" is not enough.
MixxxCollab.recentRemote = {};
MixxxCollab.ECHO_WINDOW_MS = 250;

MixxxCollab.isRemoteEcho = function(idx, value) {
    var recent = MixxxCollab.recentRemote[idx];
    if (!recent) {
        return false;
    }
    var cutoff = Date.now() - MixxxCollab.ECHO_WINDOW_MS;
    while (recent.length && recent[0][1] < cutoff) {
        recent.shift();
    }
    for (var i = 0; i < recent.length; i++) {
        if (Math.abs(recent[i][0] - value) < MixxxCollab.EPSILON) {
            return true;
        }
    }
    return false;
};

MixxxCollab.connections = [];

MixxxCollab.encodeFloat = function(value) {
    var view = new DataView(new ArrayBuffer(4));
    view.setFloat32(0, value, false);
    var u = view.getUint32(0, false);
    return [
        (u >>> 28) & 0x0F,
        (u >>> 21) & 0x7F,
        (u >>> 14) & 0x7F,
        (u >>> 7) & 0x7F,
        u & 0x7F,
    ];
};

MixxxCollab.decodeFloat = function(b) {
    var u = ((b[0] << 28) | (b[1] << 21) | (b[2] << 14) | (b[3] << 7) | b[4]) >>> 0;
    var view = new DataView(new ArrayBuffer(4));
    view.setUint32(0, u, false);
    return view.getFloat32(0, false);
};

MixxxCollab.send = function(type, idx, value) {
    var msg = [0xF0, MixxxCollab.SYSEX_ID, MixxxCollab.FROM_MIXXX, type, idx]
        .concat(MixxxCollab.encodeFloat(value))
        .concat([0xF7]);
    midi.sendSysexMsg(msg, msg.length);
};

MixxxCollab.sendValue = function(idx, value) {
    MixxxCollab.send(MixxxCollab.MSG_VALUE, idx, value);
};

// Deck number (0-based) if control idx is a synced deck's rate, else -1.
MixxxCollab.rateDeck = function(idx) {
    var c = MixxxCollab.controls[idx];
    return c[1] === "rate" ? MixxxCollab.decks.indexOf(c[0]) : -1;
};

MixxxCollab.rememberRemote = function(idx, value) {
    if (!MixxxCollab.recentRemote[idx]) {
        MixxxCollab.recentRemote[idx] = [];
    }
    MixxxCollab.recentRemote[idx].push([value, Date.now()]);
};

// Position reports: a few per second while playing; every change while
// paused (cue presses, seeks, loads), since where a paused deck sits is what
// the next play starts from.
MixxxCollab.makePositionHandler = function(deck) {
    var group = MixxxCollab.decks[deck];
    var lastValue = 0;
    var lastTime = 0;
    return function(value) {
        var now = Date.now();
        // A jump (hotcue, cue, beatjump, loop wrap) is reported straight
        // away, so the other side can follow it without waiting.
        var duration = engine.getValue(group, "duration");
        var expected = lastValue + (now - lastTime) / 1000 *
            engine.getValue(group, "rate_ratio") / (duration || 1);
        var jumped = duration > 0 && Math.abs(value - expected) * duration > MixxxCollab.JUMP_SECONDS;
        lastValue = value;
        lastTime = now;
        if (!jumped && engine.getValue(group, "play") &&
                now - MixxxCollab.lastPositionSent[deck] < MixxxCollab.POSITION_INTERVAL_MS) {
            return;
        }
        MixxxCollab.lastPositionSent[deck] = now;
        MixxxCollab.send(MixxxCollab.MSG_POSITION, deck, value);
    };
};

MixxxCollab.sendPositionNow = function(deck) {
    MixxxCollab.lastPositionSent[deck] = Date.now();
    MixxxCollab.send(MixxxCollab.MSG_POSITION, deck,
        engine.getValue(MixxxCollab.decks[deck], "playposition"));
};

// How fast the deck moves through the track, so a late play can be placed.
MixxxCollab.sendSpeed = function(deck) {
    var group = MixxxCollab.decks[deck];
    var duration = engine.getValue(group, "duration");
    if (duration > 0) {
        MixxxCollab.send(MixxxCollab.MSG_SPEED, deck, engine.getValue(group, "rate_ratio") / duration);
    }
};

// Deck number (0-based) if control idx is a synced deck's play button, else -1.
MixxxCollab.playDeck = function(idx) {
    var c = MixxxCollab.controls[idx];
    return c[1] === "play" ? MixxxCollab.decks.indexOf(c[0]) : -1;
};

// Seek to an exact position. With quantize on, Mixxx keeps the beat phase when
// a playing deck seeks, so the deck lands up to half a beat from where it was
// sent and small corrections do nothing. Turn quantize off for the seek and
// put it back once the engine has processed it.
MixxxCollab.seekExact = function(deck, position) {
    var group = MixxxCollab.decks[deck];
    if (engine.getValue(group, "quantize")) {
        engine.setValue(group, "quantize", 0);
        if (!MixxxCollab.quantizeTimer[deck]) {
            MixxxCollab.quantizeTimer[deck] = engine.beginTimer(250, function() {
                MixxxCollab.quantizeTimer[deck] = 0;
                engine.setValue(group, "quantize", 1);
            }, true);
        }
    }
    engine.setValue(group, "playposition", position);
};

// Apply a speed trim from the bridge on top of whatever the rate is set to.
MixxxCollab.applyTrim = function(deck, trim) {
    var group = MixxxCollab.decks[deck];
    var base = engine.getValue(group, "rate_ratio") / MixxxCollab.trimRatio[deck];
    MixxxCollab.trimRatio[deck] = 1 + trim;
    MixxxCollab.lastTrim[deck] = Date.now();
    engine.setValue(group, "rate_ratio", base * MixxxCollab.trimRatio[deck]);
};

MixxxCollab.sendSnapshot = function() {
    for (var i = 0; i < MixxxCollab.controls.length; i++) {
        var c = MixxxCollab.controls[i];
        MixxxCollab.sendValue(i, engine.getValue(c[0], c[1]));
    }
};

// Tell the bridge the active loop's length, so it can compare playheads
// around the loop instead of treating every wrap as a jump.
MixxxCollab.sendLoop = function(deck) {
    var group = MixxxCollab.decks[deck];
    var samples = engine.getValue(group, "track_samples");
    var length = 0;
    if (engine.getValue(group, "loop_enabled") && samples > 0) {
        length = (engine.getValue(group, "loop_end_position") -
            engine.getValue(group, "loop_start_position")) / samples;
    }
    MixxxCollab.send(MixxxCollab.MSG_LOOP, deck, Math.max(0, length));
};

MixxxCollab.makeHandler = function(idx) {
    var playDeck = MixxxCollab.playDeck(idx);
    var c = MixxxCollab.controls[idx];
    var loopDeck = c[1].indexOf("loop_") === 0 ? MixxxCollab.decks.indexOf(c[0]) : -1;
    return function(value) {
        if (loopDeck >= 0) {
            MixxxCollab.sendLoop(loopDeck);
        }
        // Play/pause: tell the bridge the speed first and the position after
        // the change, whether it came from here or from the peer, so it knows
        // exactly where this deck started or stopped.
        if (playDeck >= 0) {
            MixxxCollab.sendSpeed(playDeck);
        }
        if (!MixxxCollab.isRemoteEcho(idx, value)) {  // else it came from the peer
            MixxxCollab.sendLocalChange(idx, value);
        }
        if (playDeck >= 0) {
            MixxxCollab.sendPositionNow(playDeck);
        }
    };
};

MixxxCollab.sendLocalChange = function(idx, value) {
    var deck = MixxxCollab.rateDeck(idx);
    if (deck >= 0 && Date.now() - MixxxCollab.lastTrim[deck] < MixxxCollab.TRIM_ACTIVE_MS) {
        // This deck is being trimmed to follow the peer, so its rate
        // belongs to the peer; the change we see is our own trim.
        return;
    }
    if (deck >= 0) {
        MixxxCollab.trimRatio[deck] = 1;  // a local pitch change overwrote any old trim
    }
    MixxxCollab.sendValue(idx, value);
};

// ---- Track loading (MixxxCollab build of Mixxx only) ----

MixxxCollab.canLoad = function() {
    return typeof engine.getTrackLocation === "function" &&
        typeof engine.loadTrackFromLocation === "function";
};

MixxxCollab.toUtf8 = function(text) {
    var raw = unescape(encodeURIComponent(text));
    var bytes = [];
    for (var i = 0; i < raw.length; i++) {
        bytes.push(raw.charCodeAt(i));
    }
    return bytes;
};

MixxxCollab.fromUtf8 = function(bytes) {
    var raw = "";
    for (var i = 0; i < bytes.length; i++) {
        raw += String.fromCharCode(bytes[i]);
    }
    return decodeURIComponent(escape(raw));
};

MixxxCollab.sendPath = function(type, deck, path) {
    var bytes = MixxxCollab.toUtf8(path);
    var count = Math.max(1, Math.ceil(bytes.length / MixxxCollab.PATH_CHUNK));
    for (var n = 0; n < count; n++) {
        var msg = [0xF0, MixxxCollab.SYSEX_ID, MixxxCollab.FROM_MIXXX, type, deck, n, count];
        bytes.slice(n * MixxxCollab.PATH_CHUNK, (n + 1) * MixxxCollab.PATH_CHUNK).forEach(function(b) {
            msg.push(b >> 4, b & 0x0F);
        });
        msg.push(0xF7);
        midi.sendSysexMsg(msg, msg.length);
    }
};

// Tell the bridge which file a deck has, whenever it changes. The deck's
// controls change before Mixxx records which file is loaded, so a load shows
// up as an empty location at first: check again a few times until it's there.
MixxxCollab.reportTrack = function(deck, attempt) {
    if (!MixxxCollab.canLoad()) {
        return;
    }
    attempt = attempt || 0;
    var group = MixxxCollab.decks[deck];
    var location = engine.getTrackLocation(group);
    if (!location && engine.getValue(group, "track_loaded") && attempt < 20) {
        engine.beginTimer(100, function() {
            MixxxCollab.reportTrack(deck, attempt + 1);
        }, true);
        return;
    }
    if (location === MixxxCollab.lastLocation[deck]) {
        return;
    }
    MixxxCollab.lastLocation[deck] = location;
    MixxxCollab.sendPath(MixxxCollab.MSG_LOADED, deck, location);
};

// A chunk of a path the bridge wants loaded; load it once complete.
MixxxCollab.loadChunk = function(data, length) {
    var deck = data[4], n = data[5], count = data[6];
    if (deck >= MixxxCollab.decks.length || !count) {
        return;
    }
    var chunks = MixxxCollab.loadChunks[deck];
    if (n === 0) {
        MixxxCollab.loadChunks[deck] = chunks = {};
    }
    var bytes = [];
    for (var i = 7; i + 1 < length - 1; i += 2) {
        bytes.push((data[i] << 4) | data[i + 1]);
    }
    chunks[n] = bytes;
    var all = [];
    for (var c = 0; c < count; c++) {
        if (!chunks[c]) {
            return;  // still waiting for some
        }
        all = all.concat(chunks[c]);
    }
    MixxxCollab.loadChunks[deck] = {};
    if (!MixxxCollab.canLoad()) {
        print("MixxxCollab: can't load tracks here; this needs the MixxxCollab build of Mixxx");
        return;
    }
    engine.loadTrackFromLocation(MixxxCollab.decks[deck], MixxxCollab.fromUtf8(all), false);
};

MixxxCollab.init = function(id, debugging) {
    for (var i = 0; i < MixxxCollab.controls.length; i++) {
        var c = MixxxCollab.controls[i];
        var conn = engine.makeConnection(c[0], c[1], MixxxCollab.makeHandler(i));
        if (conn) {
            MixxxCollab.connections.push(conn);
        } else {
            print("MixxxCollab: could not connect " + c[0] + "," + c[1]);
        }
    }
    MixxxCollab.decks.forEach(function(group, d) {
        var pos = engine.makeConnection(group, "playposition", MixxxCollab.makePositionHandler(d));
        if (pos) {
            MixxxCollab.connections.push(pos);
        }
        ["track_loaded", "track_samples"].forEach(function(key) {
            var conn = engine.makeConnection(group, key, function() {
                MixxxCollab.reportTrack(d, 0);
            });
            if (conn) {
                MixxxCollab.connections.push(conn);
            }
        });
        MixxxCollab.reportTrack(d, 0);
    });
    if (!MixxxCollab.canLoad()) {
        print("MixxxCollab: stock Mixxx; track loading needs the MixxxCollab build");
    }
    print("MixxxCollab: bridge mapping ready (" + MixxxCollab.controls.length + " controls)");
};

MixxxCollab.shutdown = function() {
    for (var i = 0; i < MixxxCollab.connections.length; i++) {
        MixxxCollab.connections[i].disconnect();
    }
    MixxxCollab.connections = [];
};

// Mixxx calls this for incoming SysEx on MIDI controllers.
MixxxCollab.incomingData = function(data, length) {
    if (length < 5 || data[0] !== 0xF0 || data[1] !== MixxxCollab.SYSEX_ID) {
        return;
    }
    if (data[2] !== MixxxCollab.TO_MIXXX) {
        return;  // our own outgoing message looped back; ignore
    }

    var type = data[3];
    if (type === MixxxCollab.MSG_SNAPSHOT_REQUEST) {
        MixxxCollab.sendSnapshot();
        return;
    }
    if (type === MixxxCollab.MSG_LOAD) {
        MixxxCollab.loadChunk(data, length);
        return;
    }
    if (type === MixxxCollab.MSG_REPORT_TRACKS) {
        // The bridge (re)started and doesn't know what's loaded.
        for (var d = 0; d < MixxxCollab.decks.length; d++) {
            MixxxCollab.lastLocation[d] = null;
            MixxxCollab.reportTrack(d, 0);
        }
        return;
    }
    if (length < 11) {
        return;
    }
    var idx = data[4];
    var value = MixxxCollab.decodeFloat([data[5], data[6], data[7], data[8], data[9]]);

    if (type === MixxxCollab.MSG_SEEK || type === MixxxCollab.MSG_TRIM) {
        if (idx >= MixxxCollab.decks.length) {
            return;
        }
        if (type === MixxxCollab.MSG_SEEK) {
            MixxxCollab.seekExact(idx, value);
        } else {
            MixxxCollab.applyTrim(idx, value);
        }
        return;
    }
    if (type !== MixxxCollab.MSG_VALUE) {
        return;
    }

    var c = MixxxCollab.controls[idx];
    if (!c) {
        return;
    }
    var deck = MixxxCollab.rateDeck(idx);
    var trimmed = deck >= 0 && MixxxCollab.trimRatio[deck] !== 1;
    if (!trimmed && Math.abs(engine.getValue(c[0], c[1]) - value) < MixxxCollab.EPSILON) {
        return;  // already there
    }
    MixxxCollab.rememberRemote(idx, value);
    engine.setValue(c[0], c[1], value);
    if (trimmed) {
        // The peer's rate replaced ours, trim included; put the trim back.
        engine.setValue(c[0], "rate_ratio",
            engine.getValue(c[0], "rate_ratio") * MixxxCollab.trimRatio[deck]);
    }
};
