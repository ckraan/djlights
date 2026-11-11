"""
web_feedback.py — Mobile voting interface for DJ lighting feedback.

Runs a lightweight HTTP server on the local network (default port 5050).
Any phone on the same WiFi can open http://<laptop-ip>:5050 and vote.

Votes:
  GREAT / OK / BAD
  BAD immediately triggers an effect reroll (hard feedback).
  All votes are logged to feedback_log.json for AI context.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

_log = logging.getLogger("web_feedback")

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_HERE, "feedback_log.json")

_VOTES = ("great", "ok", "bad")

# ── Mobile HTML (served at GET /) ──────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>DJ Lights</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body {
    background: #080808;
    color: #eee;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    padding: 16px;
    gap: 14px;
    user-select: none;
  }

  /* ── Track info ── */
  #info {
    background: #111;
    border-radius: 12px;
    padding: 14px 16px;
    border: 1px solid #1e1e1e;
  }
  #track    { font-size: 18px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #artist   { font-size: 13px; color: #888; margin-top: 2px; }
  #meta     { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
  .tag {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 6px;
    padding: 3px 9px;
    font-size: 12px;
    color: #aaa;
  }
  .tag span { color: #00cc88; font-weight: 600; }
  #energy-tag { display: none; }
  #energy-tag.building { display: inline-flex; border-color: #f97316; }
  #energy-tag.building span { color: #f97316; }
  #energy-tag.dropping  { display: inline-flex; border-color: #60a5fa; }
  #energy-tag.dropping  span { color: #60a5fa; }

  /* ── Vote buttons ── */
  #buttons {
    display: flex;
    flex-direction: column;
    gap: 12px;
    flex: 1;
  }
  .vote-btn {
    flex: 1;
    min-height: 80px;
    border: none;
    border-radius: 16px;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: 1px;
    cursor: pointer;
    transition: opacity 0.1s, transform 0.08s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }
  .vote-btn:active { transform: scale(0.96); opacity: 0.85; }
  #btn-great { background: #166534; color: #4ade80; border: 2px solid #16a34a; }
  #btn-ok    { background: #78350f; color: #fbbf24; border: 2px solid #d97706; }
  #btn-bad   { background: #7f1d1d; color: #f87171; border: 2px solid #dc2626; }

  /* flash feedback */
  .flash { animation: flash 0.4s ease; }
  @keyframes flash { 0%,100% { opacity:1 } 50% { opacity:0.35 } }

  /* ── Recent votes ── */
  #recent-label { font-size: 11px; color: #444; text-transform: uppercase; letter-spacing: 1px; }
  #recent {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .vote-chip {
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
  }
  .vote-chip.great { background:#14532d; color:#4ade80; }
  .vote-chip.ok    { background:#451a03; color:#fbbf24; }
  .vote-chip.bad   { background:#450a0a; color:#f87171; }

  /* ── Offline indicator ── */
  #offline {
    display: none;
    text-align: center;
    color: #555;
    font-size: 13px;
    padding: 8px;
  }
</style>
</head>
<body>

<div id="info">
  <div id="track">—</div>
  <div id="artist"></div>
  <div id="meta">
    <div class="tag">Phrase: <span id="phrase-val">—</span></div>
    <div class="tag">Effect: <span id="effect-val">—</span></div>
    <div class="tag">Palette: <span id="palette-val">—</span></div>
    <div class="tag">BPM: <span id="bpm-val">—</span></div>
    <div class="tag" id="energy-tag">Energy: <span id="energy-val">—</span></div>
  </div>
</div>

<div id="buttons">
  <button class="vote-btn" id="btn-great" onclick="vote('great')">&#128077; GREAT</button>
  <button class="vote-btn" id="btn-ok"    onclick="vote('ok')">&#128076; OK</button>
  <button class="vote-btn" id="btn-bad"   onclick="vote('bad')">&#128078; BAD</button>
</div>

<div id="recent-label">Recent votes</div>
<div id="recent"></div>
<div id="offline">● not connected</div>

<script>
let _last_effect = null;

async function vote(v) {
  const btn = document.getElementById('btn-' + v);
  btn.classList.add('flash');
  setTimeout(() => btn.classList.remove('flash'), 420);
  try {
    await fetch('/vote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vote: v})
    });
  } catch(e) {}
  await poll();
}

async function poll() {
  try {
    const r = await fetch('/state');
    if (!r.ok) throw new Error();
    const s = await r.json();
    document.getElementById('offline').style.display = 'none';

    document.getElementById('track').textContent  = s.track  || '—';
    document.getElementById('artist').textContent = s.artist || '';
    document.getElementById('phrase-val').textContent  = s.phrase  || '—';
    document.getElementById('effect-val').textContent  = s.effect  || '—';
    document.getElementById('palette-val').textContent = s.palette || '—';
    document.getElementById('bpm-val').textContent     = s.bpm ? Math.round(s.bpm) : '—';

    const et = document.getElementById('energy-tag');
    const ev = document.getElementById('energy-val');
    et.className = '';
    if (s.energy && s.energy !== 'stable') {
      et.classList.add(s.energy);
      ev.textContent = s.energy;
    }

    const recent = document.getElementById('recent');
    recent.innerHTML = (s.recent_votes || []).map(v =>
      `<span class="vote-chip ${v.vote}">${v.vote} · ${v.effect || '?'}</span>`
    ).join('');
  } catch(e) {
    document.getElementById('offline').style.display = 'block';
  }
}

poll();
setInterval(poll, 2500);
</script>
</body>
</html>"""


# ── Shared state ───────────────────────────────────────────────────────────────

class _SharedState:
    def __init__(self):
        self._lock   = threading.Lock()
        self._state  = {
            "track": "", "artist": "", "phrase": "", "effect": "",
            "palette": "", "bpm": 0.0, "energy": "stable",
            "recent_votes": [],
        }
        self._votes  = []   # full log in memory
        self._on_bad = None # callback() called on "bad" vote

    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def get(self):
        with self._lock:
            return dict(self._state)

    def record_vote(self, vote: str):
        with self._lock:
            state = dict(self._state)
        entry = {
            "ts":      datetime.now().isoformat(timespec="seconds"),
            "vote":    vote,
            "effect":  state.get("effect", ""),
            "palette": state.get("palette", ""),
            "phrase":  state.get("phrase", ""),
            "track":   state.get("track", ""),
            "energy":  state.get("energy", "stable"),
        }
        self._votes.append(entry)
        _save_vote(entry)

        recent = [{"vote": v["vote"], "effect": v["effect"]}
                  for v in self._votes[-8:]]
        recent.reverse()
        with self._lock:
            self._state["recent_votes"] = recent

        if vote == "bad" and self._on_bad:
            try:
                self._on_bad()
            except Exception as e:
                _log.warning("on_bad callback failed: %s", e)

    def feedback_summary(self, n=60) -> str:
        """Return a human-readable summary of recent votes for AI context."""
        recent = self._votes[-n:]
        if not recent:
            return None
        counts = {}  # (effect, phrase) → {great:0, ok:0, bad:0}
        for v in recent:
            key = (v.get("effect", "?"), v.get("phrase", "?"))
            if key not in counts:
                counts[key] = {"great": 0, "ok": 0, "bad": 0}
            vt = v.get("vote", "ok")
            if vt in counts[key]:
                counts[key][vt] += 1
        parts = []
        for (effect, phrase), c in counts.items():
            total = sum(c.values())
            if total == 0:
                continue
            parts.append(
                f"{effect} on {phrase}: "
                f"{c['great']}✓ {c['ok']}~ {c['bad']}✗"
            )
        return "; ".join(parts) if parts else None


def _save_vote(entry: dict):
    try:
        log = []
        if os.path.exists(_LOG_PATH):
            with open(_LOG_PATH) as f:
                log = json.load(f)
        log.append(entry)
        with open(_LOG_PATH, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        _log.warning("vote save failed: %s", e)


def _load_votes() -> list:
    try:
        if os.path.exists(_LOG_PATH):
            with open(_LOG_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []


# ── HTTP handler ───────────────────────────────────────────────────────────────

def _make_handler(shared: _SharedState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silence access log

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/state":
                data = json.dumps(shared.get()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                body = _HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/vote":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    vote = json.loads(body).get("vote", "")
                    if vote in _VOTES:
                        shared.record_vote(vote)
                except Exception:
                    pass
                self.send_response(204)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Public API ─────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Best-guess local WiFi IP."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _print_qr(url: str):
    """Print a QR code for url to stdout using Unicode half-blocks."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        print(f"\n  Feedback page: {url}\n")
        # Two rows per terminal line using upper/lower half-block characters
        rows = len(matrix)
        for y in range(0, rows, 2):
            line = "  "
            for x in range(len(matrix[y])):
                top = matrix[y][x]
                bot = matrix[y + 1][x] if y + 1 < rows else False
                if top and bot:     line += "█"
                elif top:           line += "▀"
                elif bot:           line += "▄"
                else:               line += " "
            print(line)
        print()
    except Exception:
        print(f"\n  Feedback page: {url}\n")


class FeedbackServer:
    """
    Start with FeedbackServer(on_bad_vote=callback, port=5050).
    Call update_state(**kwargs) from the render/UI loop to keep the page fresh.
    Call feedback_summary() to get a string for AI context.
    """

    def __init__(self, on_bad_vote=None, port=5050):
        self._shared       = _SharedState()
        self._shared._on_bad = on_bad_vote
        self._port         = port
        self._server       = None
        self._thread       = None

        # Pre-load historical votes for summary continuity
        self._shared._votes = _load_votes()

    def start(self):
        try:
            self._server = _ThreadedHTTPServer(("", self._port), _make_handler(self._shared))
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            url = f"http://{_local_ip()}:{self._port}"
            _log.info("Feedback server on %s", url)
            _print_qr(url)
        except OSError as e:
            _log.warning("Feedback server failed to start: %s", e)

    def stop(self):
        if self._server:
            self._server.shutdown()

    def update_state(self, track="", artist="", phrase="", effect="",
                     palette="", bpm=0.0, energy="stable"):
        self._shared.update(
            track=track, artist=artist, phrase=phrase,
            effect=effect, palette=palette, bpm=bpm, energy=energy,
        )

    def feedback_summary(self, n=60) -> str:
        return self._shared.feedback_summary(n)
