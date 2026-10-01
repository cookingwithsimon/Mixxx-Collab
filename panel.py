"""The session panel: a small local web page served by the bridge.

It shows the partner, the link, the clock, who holds the crossfader and
master, and each deck's owner, track and sync state, with Take control /
Hand over buttons and a box for the partner's reply code. It listens on
127.0.0.1 only, so nothing outside this machine can see or press it.
See "Session panel" in the integration plan.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MixxxCollab</title>
<style>
  :root { --bg:#16181d; --card:#20232a; --line:#2e323b; --text:#e8eaef; --dim:#9aa1ad;
          --green:#3ccf7e; --amber:#f2b33d; --red:#ef5b5b; --accent:#6c8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width:760px; margin:0 auto; padding:16px; }
  h1 { font-size:18px; margin:0 0 12px; font-weight:600; }
  .row { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .label { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .value { font-size:17px; margin-top:4px; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px;
         vertical-align:middle; background:var(--dim); }
  .green{background:var(--green)} .amber{background:var(--amber)} .red{background:var(--red)}
  section { margin-top:14px; }
  .token { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  .token .value { font-size:20px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:10px 16px;
           font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled { background:var(--line); color:var(--dim); cursor:default; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:12px; text-transform:uppercase; }
  td.track { color:var(--dim); word-break:break-word; }
  .code { font-family:ui-monospace, Consolas, monospace; background:var(--bg); padding:8px;
          border-radius:6px; word-break:break-all; user-select:all; }
  input { width:100%; background:var(--bg); color:var(--text); border:1px solid var(--line);
          border-radius:6px; padding:9px; font-family:ui-monospace, Consolas, monospace; }
  .notices div { padding:4px 0; color:var(--dim); }
  .notices div:first-child { color:var(--text); }
  .offline { opacity:.55; }
</style></head>
<body><main>
  <h1>MixxxCollab session</h1>
  <div class="row">
    <div class="card"><div class="label">Partner</div><div class="value" id="partner">–</div></div>
    <div class="card"><div class="label">Link</div><div class="value" id="link">–</div></div>
    <div class="card"><div class="label">Clock</div><div class="value" id="clock">–</div></div>
    <div class="card"><div class="label">This machine</div><div class="value" id="role">–</div></div>
  </div>

  <section class="card token">
    <div><div class="label">Crossfader and master</div><div class="value" id="holder">–</div></div>
    <div><button id="tokenbtn" disabled>–</button></div>
  </section>

  <section class="card">
    <div class="label" style="margin-bottom:6px">Decks</div>
    <table><thead><tr><th>Deck</th><th>Owner</th><th>State</th><th>Track</th></tr></thead>
    <tbody id="decks"></tbody></table>
  </section>

  <section class="card" id="codes" hidden>
    <div class="label">Codes</div>
    <div id="invite" hidden><p>Invite code for your partner:</p><div class="code" id="invitecode"></div></div>
    <div id="reply" hidden><p>Reply code to send to the leader:</p><div class="code" id="replycode"></div></div>
    <div id="replyin" hidden><p>Partner's reply code (only needed if you don't connect within a few seconds):</p>
      <form id="replyform" style="display:flex;gap:8px"><input id="replyinput" placeholder="mxr1-…">
      <button type="submit">Use</button></form></div>
  </section>

  <section class="card"><div class="label">Recent</div><div class="notices" id="notices"></div></section>
</main>
<script>
const $ = id => document.getElementById(id);
let state = null;
function dot(c) { return '<span class="dot ' + c + '"></span>'; }
function esc(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }
async function post(path, body) {
  await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
  refresh();
}
$('tokenbtn').onclick = () => {
  if (!state) return;
  post(state.token.mine ? '/handover' : '/take');
};
$('replyform').onsubmit = e => { e.preventDefault(); post('/reply', {code:$('replyinput').value.trim()});
                                  $('replyinput').value = ''; };
function render(s) {
  state = s;
  document.body.classList.toggle('offline', !s.connected);
  $('partner').innerHTML = s.connected ? dot('green') + 'Online'
      : dot('red') + (s.last_seen_s == null ? 'Waiting' : 'Offline (' + Math.round(s.last_seen_s) + ' s)');
  const q = !s.connected ? 'red' : (s.rtt_ms != null && s.rtt_ms < 30 ? 'green' : 'amber');
  $('link').innerHTML = dot(q) + (s.rtt_ms != null && s.connected ? s.rtt_ms.toFixed(1) + ' ms' : '–')
      + (s.internet ? ' <span style="color:var(--dim);font-size:13px">internet</span>' : '');
  $('clock').innerHTML = s.clock_locked ? dot('green') + 'Locked' : dot('amber') + 'Syncing';
  $('role').textContent = s.leader ? 'Leader' : 'Follower';
  const t = s.token;
  $('holder').innerHTML = t.mine ? dot('green') + 'You' : dot('amber') + 'Partner';
  const btn = $('tokenbtn');
  btn.disabled = !s.connected || !!t.pending;
  btn.textContent = t.pending ? 'Waiting…' : (t.mine ? 'Hand over' : 'Take control');
  $('decks').innerHTML = s.decks.map(d =>
    '<tr><td>' + d.deck + '</td><td>' + (d.mine ? 'You' : 'Partner') + '</td><td>'
    + dot(d.colour) + esc(d.state) + '</td><td class="track">' + esc(d.track || '–') + '</td></tr>').join('');
  $('codes').hidden = !(s.invite || s.reply || s.wants_reply);
  $('invite').hidden = !s.invite; $('invitecode').textContent = s.invite || '';
  $('reply').hidden = !s.reply; $('replycode').textContent = s.reply || '';
  $('replyin').hidden = !s.wants_reply;
  $('notices').innerHTML = s.notices.slice().reverse().map(n =>
    '<div>' + esc(n.ago) + ' · ' + esc(n.text) + '</div>').join('') || '<div>Nothing yet</div>';
}
async function refresh() {
  try { render(await (await fetch('/state')).json()); }
  catch (e) { $('partner').innerHTML = dot('red') + 'Bridge not running'; }
}
refresh(); setInterval(refresh, 500);
</script>
</body></html>
"""


def ago(seconds):
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


def start(bridge, port):
    """Serve the panel for bridge on 127.0.0.1:port; returns the URL."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, kind="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self.reply(200, PAGE, "text/html")
            elif self.path == "/state":
                self.reply(200, json.dumps(bridge.panel_state()))
            else:
                self.reply(404, "{}")

        def do_POST(self):
            # Only pages served from here may press buttons: a browser sends
            # this header for cross-site requests, which we refuse.
            origin = self.headers.get("Origin")
            if origin and not origin.startswith((f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
                self.reply(403, "{}")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            ok = bridge.panel_action(self.path.strip("/"), body)
            self.reply(200 if ok else 400, json.dumps({"ok": ok}))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/"
