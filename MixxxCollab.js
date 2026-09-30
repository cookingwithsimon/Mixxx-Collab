// MixxxCollab bridge mapping — proof of concept
//
// Streams local control changes out as SysEx to the companion app, and applies
// changes coming back from the companion app (i.e. from the remote peer).
//
// SysEx format (all bytes after F0 must be < 0x80):
//   F0 7D <dir> <type> [<idx> <v0> <v1> <v2> <v3> <v4>] F7
//   dir:  0x01 = from Mixxx, 0x02 = to Mixxx
//   type: 0x01 = control value, 0x02 = snapshot request (to Mixxx only)
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
MixxxCollab.EPSILON = 1e-4;

// ORDER MATTERS — the index is what goes over the wire.
// Keep in sync with CONTROLS in collab_bridge.py.
MixxxCollab.controls = [
    ["[Master]", "crossfader"],

    ["[Channel1]", "play"],
    ["[Channel1]", "volume"],
    ["[Channel1]", "pregain"],
    ["[Channel1]", "rate"],
    ["[EqualizerRack1_[Channel1]_Effect1]", "parameter1"],  // low
    ["[EqualizerRack1_[Channel1]_Effect1]", "parameter2"],  // mid
    ["[EqualizerRack1_[Channel1]_Effect1]", "parameter3"],  // high
    ["[QuickEffectRack1_[Channel1]]", "super1"],            // filter

    ["[Channel2]", "play"],
    ["[Channel2]", "volume"],
    ["[Channel2]", "pregain"],
    ["[Channel2]", "rate"],
    ["[EqualizerRack1_[Channel2]_Effect1]", "parameter1"],
    ["[EqualizerRack1_[Channel2]_Effect1]", "parameter2"],
    ["[EqualizerRack1_[Channel2]_Effect1]", "parameter3"],
    ["[QuickEffectRack1_[Channel2]]", "super1"],
];

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

MixxxCollab.sendValue = function(idx, value) {
    var msg = [0xF0, MixxxCollab.SYSEX_ID, MixxxCollab.FROM_MIXXX, MixxxCollab.MSG_VALUE, idx]
        .concat(MixxxCollab.encodeFloat(value))
        .concat([0xF7]);
    midi.sendSysexMsg(msg, msg.length);
};

MixxxCollab.sendSnapshot = function() {
    for (var i = 0; i < MixxxCollab.controls.length; i++) {
        var c = MixxxCollab.controls[i];
        MixxxCollab.sendValue(i, engine.getValue(c[0], c[1]));
    }
};

MixxxCollab.makeHandler = function(idx) {
    return function(value) {
        if (MixxxCollab.isRemoteEcho(idx, value)) {
            return;  // this change came from the peer; don't echo it
        }
        MixxxCollab.sendValue(idx, value);
    };
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
    if (type !== MixxxCollab.MSG_VALUE || length < 11) {
        return;
    }

    var idx = data[4];
    var c = MixxxCollab.controls[idx];
    if (!c) {
        return;
    }
    var value = MixxxCollab.decodeFloat([data[5], data[6], data[7], data[8], data[9]]);
    if (Math.abs(engine.getValue(c[0], c[1]) - value) < MixxxCollab.EPSILON) {
        return;  // already there
    }
    if (!MixxxCollab.recentRemote[idx]) {
        MixxxCollab.recentRemote[idx] = [];
    }
    MixxxCollab.recentRemote[idx].push([value, Date.now()]);
    engine.setValue(c[0], c[1], value);
};
