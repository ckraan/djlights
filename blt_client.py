"""
blt_client.py - Single source of truth for BLT data

Data priority:
1. OSC /blt/beat → beat_number, is_playing (REAL-TIME)
2. OSC /blt/status → on_air, master, sync
3. UDP track_meta → phrases, cues, metadata (on track load)
4. HTTP params.json → fallback + loop status

Robustness features:
- MetadataCache: persists phrases to metadata_cache.json (next to this file)
- Phrase retry: polls every 500ms up to 10× when phrases arrive empty
- Phrase heartbeat: synthesises beat_number from dead-reckoned pos when OSC stale
- UDP jitter tracker: rolling 10-sample window → get_udp_jitter_ms()
- Diagnostics: status_summary() → one-line console report
"""

import collections
import json
import logging
import logging.handlers
import os
import socket
import threading
import time
from typing import Dict, Optional

import requests
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

# ── Diagnostic logger (shared with vaultfx_monitor via root) ──────────────────
_DIAG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "djlights_diag.log")
os.makedirs(os.path.dirname(_DIAG_LOG), exist_ok=True)
_diag_handler = logging.handlers.RotatingFileHandler(
    _DIAG_LOG, maxBytes=5_000_000, backupCount=2, encoding="utf-8"
)
_diag_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S"
))
_diag_handler.setLevel(logging.DEBUG)
diag = logging.getLogger("vfx.diag")
diag.setLevel(logging.DEBUG)
diag.addHandler(_diag_handler)
diag.propagate = True   # also flows into root logger → djlights_YYYYMMDD.log

from player_state import PlayerSnapshot, Phrase, CuePoint


class _PlayerState:
    """Internal mutable state - never exposed directly"""
    def __init__(self, player_num: int):
        self.player = player_num

        # Track identity
        self.track_id = 0
        self.title = ""
        self.artist = ""

        # Beat tracking (OSC authority)
        self.beat_number = 0
        self.beat_within_bar = 0
        self.is_playing = False
        self.bpm = 0.0
        self.last_beat_time = 0.0
        self.last_jump_time = 0.0   # suppress watchdog for 3s after a position jump

        # Track structure (UDP authority)
        self.track_bpm = 0.0
        self.duration_ms = 0.0
        self.phrases = []
        self.cues = []

        # Status (OSC /blt/status authority)
        self.on_air = False
        self.is_master = False
        self.is_synced = False

        # Fallback from params.json
        self.is_looping = False
        self.key = ""
        self.genre = ""
        self.mood = ""

        # Media
        self.waveform_url = ""
        self.artwork_url = ""

        # Waveform energy (EMA-smoothed, updated every beat)
        self.bass_energy   = 0.0   # 0-1, low frequency
        self.mid_energy    = 0.0   # 0-1, mid frequency
        self.treble_energy = 0.0   # 0-1, high frequency

        # Beat-anchor position (set on every /blt/beat — rock solid)
        self.beat_anchor_pos_ms = 0.0   # track position when last beat fired
        self.beat_anchor_time   = 0.0   # wall clock when last beat fired
        self.beat_anchor_bpm    = 0.0   # BPM at that beat (for dead-reckoning)

        # Authoritative phrase from BLT state file (written every beat)
        self.phrase_kind_blt   = ""
        self.beats_to_next_blt = 0

        # Phrase stabilization
        self.last_phrase_index    = -1
        self.phrase_lock_until_beat = 0
        self.osc_phrase: Optional[Phrase] = None  # set directly by /blt/phrase

        # Diagnostics: where phrases came from for this track
        # Values: "live" | "cache" | "fallback" | "retry" | ""
        self.phrases_source: str = ""
        # True when BLT signalled that phrases are not yet ready
        self.phrases_pending: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Metadata cache — persisteert phrases/cues naar schijf zodat herlaadbare
# tracks meteen phrases hebben zonder te wachten op ATF.
# ─────────────────────────────────────────────────────────────────────────────

class _MetadataCache:
    """JSON-backed cache voor track-metadata.

    Sleutel: str(track_id).  Bevat title, artist, phrases (serialiseerbaar),
    cues (serialiseerbaar) en tijdstip van opslaan.

    Thread-safe via een eigen lock; I/O gebeurt via een achtergrondthread zodat
    de UDP/OSC hot-path nooit blokkeert op disk-schrijven.
    """

    _CACHE_VERSION = 2

    def __init__(self, path: str = "") -> None:
        if not path:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata_cache.json")
        self._path   = path
        self._lock   = threading.Lock()
        self._data: dict = {}   # track_id (str) → entry dict
        self._dirty  = False
        self._load()

        # Background writer: flush every 10s if dirty
        t = threading.Thread(target=self._flush_loop, daemon=True,
                             name="meta-cache-flush")
        t.start()

    # ── Read ──────────────────────────────────────────────────────────────

    def get_phrases(self, track_id: int) -> list:
        """Geef gecachede Phrase-dicts terug, of [] als niet aanwezig."""
        with self._lock:
            entry = self._data.get(str(track_id))
            return list(entry["phrases"]) if entry else []

    def get_cues(self, track_id: int) -> list:
        """Geef gecachede cue-dicts terug, of [] als niet aanwezig."""
        with self._lock:
            entry = self._data.get(str(track_id))
            return list(entry.get("cues", [])) if entry else []

    def has_phrases(self, track_id: int) -> bool:
        with self._lock:
            entry = self._data.get(str(track_id))
            return bool(entry and entry.get("phrases"))

    # ── Write ─────────────────────────────────────────────────────────────

    def store(self, track_id: int, title: str, artist: str,
              phrases: list, cues: list) -> None:
        """Sla phrases op als ze non-empty zijn. Altijd non-blocking."""
        if not phrases:
            return
        key   = str(track_id)
        entry = {
            "track_id": track_id,
            "title":    title,
            "artist":   artist,
            "phrases":  phrases,   # list of dicts (JSON-serialiseerbaar)
            "cues":     cues,
            "saved_at": time.time(),
            "version":  self._CACHE_VERSION,
        }
        with self._lock:
            self._data[key] = entry
            self._dirty = True

    # ── Internal ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Verwijder entries van andere cache-versies
            self._data = {k: v for k, v in raw.items()
                          if isinstance(v, dict)
                          and v.get("version") == self._CACHE_VERSION
                          and v.get("phrases")}
            diag.info("[MetadataCache] geladen: %d tracks uit %s",
                      len(self._data), self._path)
        except FileNotFoundError:
            diag.info("[MetadataCache] geen cache-bestand gevonden, start leeg")
        except Exception as e:
            diag.warning("[MetadataCache] laad-fout: %s", e)

    def _flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._data)
            self._dirty = False
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
            diag.debug("[MetadataCache] geflusht: %d tracks", len(snapshot))
        except Exception as e:
            diag.warning("[MetadataCache] schrijf-fout: %s", e)

    def _flush_loop(self) -> None:
        while True:
            time.sleep(10.0)
            self._flush()


# ─────────────────────────────────────────────────────────────────────────────
# UDP jitter tracker
# ─────────────────────────────────────────────────────────────────────────────

class _JitterTracker:
    """Berekent UDP-jitter uit de inter-packet tijden.

    Jitter = gemiddeld absolute verschil van opeenvolgende inter-packet
    intervallen (RFC 3550 definitie, vereenvoudigd).
    """

    def __init__(self, window: int = 16) -> None:
        self._times: collections.deque = collections.deque(maxlen=window)

    def record(self) -> None:
        self._times.append(time.perf_counter())

    def jitter_ms(self) -> float:
        """Geef jitter in milliseconden, of 0.0 als te weinig data."""
        t = list(self._times)
        if len(t) < 3:
            return 0.0
        intervals = [t[i+1] - t[i] for i in range(len(t)-1)]
        diffs     = [abs(intervals[i+1] - intervals[i]) for i in range(len(intervals)-1)]
        return (sum(diffs) / len(diffs)) * 1000.0 if diffs else 0.0


class BLTClient:
    """
    Single point of truth for CDJ data.

    Priority:
    - OSC /blt/beat = AUTHORITATIVE for beat_number
    - UDP track_meta = AUTHORITATIVE for phrases/cues
    - HTTP params.json = fallback only
    """

    def __init__(self, osc_port=9000, udp_port=9001, blt_base="http://localhost:17081"):
        self._players: Dict[int, _PlayerState] = {
            n: _PlayerState(n) for n in range(1, 5)
        }
        self._lock = threading.Lock()
        self._osc_port = osc_port
        self._udp_port = udp_port
        self._blt_base = blt_base

        # Health tracking
        self._last_osc_beat = 0.0
        self._last_udp = 0.0
        self._last_http = 0.0
        self._last_state = 0.0

        # Metadata cache (JSON-backed phrase persistence)
        self._meta_cache = _MetadataCache()

        # UDP jitter tracker
        self._jitter = _JitterTracker(window=16)

        # (no render-thread smooth state needed — beat anchor handles it)

        # Optional callback: fired immediately on /blt/phrase from OSC thread
        # signature: cb(player_num: int, phrase_kind: str)
        self.on_phrase: Optional[callable] = None

        print("[BLTClient] Initialized")

        # Start all listeners
        self._start_osc()
        self._start_udp()
        self._start_http_poller()
        self._start_state_poller()
        self._start_diagnostics_logger()

    # ── OSC listener ──────────────────────────────────────────────────────────

    def _start_osc(self):
        """Start OSC server - PRIMARY source for beat tracking"""
        dispatcher = Dispatcher()
        dispatcher.map("/blt/beat",          self._on_osc_beat)
        dispatcher.map("/blt/status",        self._on_osc_status)
        dispatcher.map("/blt/phrase",        self._on_osc_phrase)
        dispatcher.map("/blt/track-loading", self._on_osc_track_loading)
        dispatcher.map("/blt/unload",        self._on_osc_unload)

        def run_server():
            server = BlockingOSCUDPServer(("0.0.0.0", self._osc_port), dispatcher)
            print(f"[BLT] OSC listening on port {self._osc_port}")
            server.serve_forever()

        threading.Thread(target=run_server, daemon=True, name="blt-osc").start()

    def _on_osc_beat(self, addr, dev, bpm, pitch, pos, beat_n, bar_b,
                     on_air, playing, master, sync, *args):
        """PRIMARY source for beat_number - fires every beat"""
        now = time.time()
        self._last_osc_beat = now

        with self._lock:
            ps = self._players.get(int(dev))
            if not ps:
                return

            # Accept all beat_number values from BLT OSC — it's reliable.
            # Backward jumps are legitimate (loops, seeks, cue jumps).
            bn      = int(beat_n)
            bpm_val = float(bpm)
            if bn > 0:
                ps.beat_number = bn
            ps.beat_within_bar = int(bar_b)
            ps.is_playing      = bool(playing)
            ps.last_beat_time  = now

            # Beat-anchor position.
            # Prefer BeatGrid pos from OSC (Clojure now sends BeatGrid.getTimeWithinTrack,
            # which accounts for pre-roll and tempo variations — exactly what CDJ displays).
            # Fall back to linear formula if BeatGrid unavailable (pos=0).
            if bn > 0 and bpm_val > 0:
                bg = float(pos)
                new_anchor = bg if bg > 0 else (bn - 1) * (60000.0 / bpm_val)
                old_anchor = ps.beat_anchor_pos_ms
                ps.beat_anchor_pos_ms = new_anchor
                ps.beat_anchor_time   = now
                ps.beat_anchor_bpm    = bpm_val
                # Log big jumps (cue / seek) and every 16 beats for position tracking
                jump = abs(new_anchor - old_anchor) if old_anchor > 0 else 0
                beat_dur = 60000.0 / bpm_val if bpm_val > 0 else 500.0
                if jump > beat_dur * 2.5:
                    ps.last_jump_time = now  # suppress watchdog: CDJ is beat-jumping
                if jump > 300 or bn % 16 == 1:
                    diag.info("P%d BEAT bn=%d bpm=%.1f pos=%.0fms bg=%.0f jump=%.0fms tid=%d",
                              int(dev), bn, bpm_val, new_anchor, bg, jump, ps.track_id)

            # BPM from beat message (real-time with pitch adjustment)
            if bpm_val > 0:
                ps.bpm = bpm_val

            # Status flags
            ps.on_air    = bool(on_air)
            ps.is_master = bool(master)
            ps.is_synced = bool(sync)

            # Waveform energy: args order after sync = bass, mid, treble, amp, ...
            if len(args) >= 3:
                _ema = 0.35  # EMA alpha — ~3 beats of smoothing
                ps.bass_energy   = ps.bass_energy   * (1 - _ema) + float(args[0]) * _ema
                ps.mid_energy    = ps.mid_energy    * (1 - _ema) + float(args[1]) * _ema
                ps.treble_energy = ps.treble_energy * (1 - _ema) + float(args[2]) * _ema


    def _on_osc_status(self, addr, dev, bpm, pitch, pos, remain,
                       on_air, play, mast, sync, looping=0):
        """Status updates every ~200ms — TimeFinder interpolated position"""
        now = time.time()
        with self._lock:
            ps = self._players.get(int(dev))
            if not ps:
                return

            ps.on_air    = bool(on_air)
            ps.is_master = bool(mast)
            ps.is_synced = bool(sync)

            bpm_val = float(bpm)
            if bpm_val > 0:
                ps.bpm = bpm_val
            # Status fires every 200ms for all players, including stopped ones —
            # this is the only way to clear is_playing when a deck stops.
            ps.is_playing = bool(play)

            # Use TimeFinder pos to keep anchor accurate.
            # Always re-anchor if status pos drifts > 50ms from dead-reckoning.
            # This corrects frozen-trigger drift (6s stale) as soon as status resumes.
            pos_ms = float(pos)
            if pos_ms > 0 and bpm_val > 0 and bool(play):
                if ps.beat_anchor_time > 0 and ps.beat_anchor_bpm > 0:
                    _beat_dur = 60000.0 / ps.beat_anchor_bpm
                    _elapsed  = (now - ps.beat_anchor_time) * 1000.0
                    _dr_pos   = ps.beat_anchor_pos_ms + min(_elapsed, _beat_dur * 30.0)
                else:
                    _dr_pos = ps.beat_anchor_pos_ms
                jump_ms = pos_ms - _dr_pos
                if abs(jump_ms) > 50.0:
                    # Always re-anchor for position accuracy (fixes 6s drift).
                    ps.beat_anchor_pos_ms = pos_ms
                    ps.beat_anchor_time   = now
                    ps.beat_anchor_bpm    = bpm_val
                    # Only update beat_number on large jumps (cue click/seek).
                    # Routine 50ms corrections must NOT touch beat_number —
                    # that causes the phrase candidate to flicker every 200ms → LEDs break.
                    if abs(jump_ms) > 300.0:
                        approx_beat = max(1, int(pos_ms / (60000.0 / bpm_val)) + 1)
                        ps.beat_number = approx_beat
                        ps.last_jump_time = now   # suppress watchdog for 3s
                        diag.info("P%d STATUS-JUMP pos=%.0fms dr=%.0fms delta=%.0fms approx_bn=%d tid=%d",
                                  int(dev), pos_ms, _dr_pos, jump_ms, approx_beat, ps.track_id)

    def _on_osc_phrase(self, addr, dev, p_idx, p_kind, p_disp,
                       beat_start, beat_end, p_sec, *args):
        """Direct phrase notification from BLT — fires immediately on phrase boundary."""
        player_num = int(dev)
        kind       = str(p_kind)
        with self._lock:
            ps = self._players.get(player_num)
            if not ps:
                return
            phrase = Phrase(
                index      = int(p_idx),
                beat_start = int(beat_start),
                beat_end   = int(beat_end),
                kind       = kind,
                display    = str(p_disp),
            )
            ps.osc_phrase           = phrase
            ps.last_phrase_index    = int(p_idx)
            ps.phrase_lock_until_beat = int(beat_end) if int(beat_end) > 0 else ps.beat_number + 32
        # Fire callback outside the lock so it can call get_snapshot() without deadlock
        cb = self.on_phrase
        if cb:
            try:
                cb(player_num, kind)
            except Exception:
                pass

    def _on_osc_track_loading(self, addr, dev, track_id):
        """Track load started — reset position state so old track data doesn't bleed through."""
        with self._lock:
            ps = self._players.get(int(dev))
            if not ps:
                return
            old_tid = ps.track_id
            ps.track_id           = int(track_id)
            ps.beat_number        = 0
            ps.beat_anchor_pos_ms = 0.0
            ps.beat_anchor_time   = 0.0
            ps.beat_anchor_bpm    = 0.0
            ps.phrases            = []
            ps.osc_phrase         = None
            ps.phrase_kind_blt    = ""
            ps.beats_to_next_blt  = 0
            ps.duration_ms        = 0.0
            ps.last_beat_time     = 0.0
            ps.phrases_source     = ""
            ps.phrases_pending    = False
            # Clear metadata so render/scan loops don't use stale title/waveform
            # for the new track_id. UDP meta will repopulate these shortly.
            ps.title              = ""
            ps.artist             = ""
            ps.waveform_url       = ""
            ps.artwork_url        = ""
        new_tid = int(track_id)
        diag.info("P%d TRACK-LOADING old_tid=%d new_tid=%d — state reset",
                  int(dev), old_tid, new_tid)
        # Schedule background retry loop: polls every 500ms up to 10× until
        # live phrases arrive.  Falls back to cache after 5s.
        if new_tid > 0:
            t = threading.Thread(
                target=self._retry_phrases_for_player,
                args=(int(dev), new_tid),
                daemon=True,
                name=f"phrase-retry-P{int(dev)}",
            )
            t.start()

    def _on_osc_unload(self, addr, dev):
        """Track unloaded - reset to empty state"""
        diag.info("P%d TRACK-UNLOAD", int(dev))
        with self._lock:
            ps = self._players.get(int(dev))
            if not ps:
                return

            ps.track_id        = 0
            ps.title           = ""
            ps.artist          = ""
            ps.beat_number     = 0
            ps.beat_within_bar = 0
            ps.is_playing      = False
            ps.track_bpm       = 0.0
            ps.duration_ms     = 0.0
            ps.phrases         = []
            ps.cues            = []
            ps.waveform_url    = ""
            ps.artwork_url     = ""
            ps.osc_phrase          = None
            ps.beat_anchor_pos_ms  = 0.0
            ps.beat_anchor_time    = 0.0
            ps.beat_anchor_bpm     = 0.0
            ps.phrase_kind_blt     = ""
            ps.beats_to_next_blt   = 0

    # ── UDP track_meta listener ───────────────────────────────────────────────

    def _start_udp(self):
        """Start UDP listener - AUTHORITATIVE for track metadata"""
        def run_receiver():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self._udp_port))
            sock.settimeout(1.0)
            print(f"[BLT] UDP listening on port {self._udp_port}")

            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    self._handle_udp(data)
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[BLT UDP] {e}")

        threading.Thread(target=run_receiver, daemon=True, name="blt-udp").start()

    def _handle_udp(self, data: bytes):
        """Handle UDP track_meta packets - AUTHORITATIVE for phrases/cues"""
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return

        if msg.get("type") != "track_meta":
            return

        self._last_udp = time.time()
        self._jitter.record()
        dev = int(msg.get("player", 0))

        with self._lock:
            ps = self._players.get(dev)
            if not ps:
                return

            # Track metadata
            ps.title    = msg.get("title", "") or ""
            ps.artist   = msg.get("artist", "") or ""
            # Only update track_id if positive — BLT sometimes sends tid=-1 while
            # track is still loading; writing -1 drops the player from has_track checks
            udp_tid = int(msg.get("track_id", 0) or 0)
            if udp_tid > 0:
                ps.track_id = udp_tid

            # Track structure - AUTHORITATIVE
            # Clojure sends key "bpm" (track BPM from rekordbox metadata)
            bpm_val = float(msg.get("bpm", 0) or 0)
            if bpm_val > 0:
                ps.track_bpm = bpm_val
            dur = float(msg.get("duration_ms", 0) or 0)
            if dur > 0:
                ps.duration_ms = dur

            # Parse phrases
            phrases = []
            for p in msg.get("phrases", []):
                try:
                    phrases.append(Phrase(
                        index      = int(p.get("index", 0)),
                        beat_start = int(p.get("beat_start", 0)),
                        beat_end   = int(p.get("beat_end", 0)),
                        kind       = str(p.get("kind", "")),
                        display    = str(p.get("display", "")),
                        fill       = bool(p.get("fill", False)),
                        fill_beat  = int(p.get("fill_beat", 0)),
                    ))
                except Exception as e:
                    print(f"[BLT UDP] phrase parse error: {e}")

            # phrases_pending: BLT signals ATF not yet loaded
            ps.phrases_pending = bool(msg.get("phrases_pending", False))

            if phrases:
                ps.phrases = phrases
                ps.phrases_source = "live"
                # Persist to disk so future loads can use cache
                phrases_dicts = [
                    {"index": p.index, "beat_start": p.beat_start,
                     "beat_end": p.beat_end, "kind": p.kind,
                     "display": p.display, "fill": p.fill,
                     "fill_beat": p.fill_beat}
                    for p in phrases
                ]
                cues_dicts_for_cache = msg.get("cues", [])
                self._meta_cache.store(
                    ps.track_id, ps.title, ps.artist,
                    phrases_dicts, cues_dicts_for_cache,
                )
            elif ps.phrases_pending or not ps.phrases:
                # Phrases empty — try cache immediately as a pre-fill.
                # Retry poller (_retry_phrases_for_player) will overwrite with live data.
                cached = self._meta_cache.get_phrases(ps.track_id)
                if cached and not ps.phrases:
                    ps.phrases = self._phrases_from_dicts(cached)
                    ps.phrases_source = "cache"
                    diag.info("P%d UDP-META tid=%d — phrases from CACHE (pending=%s count=%d)",
                              dev, ps.track_id, ps.phrases_pending, len(cached))

            diag.info("P%d UDP-META tid=%d title=%r dur=%.0fms phrases=%d [src=%s] cues_raw=%d pending=%s",
                      dev, ps.track_id, ps.title, ps.duration_ms,
                      len(ps.phrases), ps.phrases_source or "—",
                      len(msg.get("cues", [])), ps.phrases_pending)

            # Parse cues
            cues = []
            for c in msg.get("cues", []):
                try:
                    col = c.get("color", [0, 0, 0])
                    if not isinstance(col, (list, tuple)):
                        col = [0, 0, 0]
                    cues.append(CuePoint(
                        index       = int(c.get("index", 0)),
                        label       = str(c.get("label", "")),
                        name        = str(c.get("name", "")),
                        type        = str(c.get("type", "cue")),
                        time_ms     = int(c.get("time_ms", 0)),
                        beat        = int(c.get("beat", 0)),
                        color       = tuple(col[:3]),
                        loop_end_ms = int(c.get("loop_end_ms", 0)),
                    ))
                except Exception as e:
                    print(f"[BLT UDP] cue parse error: {e}")

            if cues:
                ps.cues = cues

            # Additional metadata
            mood = msg.get("mood", "")
            if mood:
                ps.mood = str(mood)

            genre = msg.get("genre", "")
            if genre:
                ps.genre = str(genre)

            key = msg.get("key", "")
            if key:
                ps.key = str(key)

            # Waveform URL (relative → absolute via BLT base)
            wf = msg.get("waveform_overview_url", "")
            if wf:
                ps.waveform_url = self._blt_base + wf

            # Artwork: prefer local file path (BLT saves /tmp/vp_art_P{n}.jpg),
            # fall back to relative HTTP URL
            art_path = msg.get("artwork_path", "")
            art_url  = msg.get("artwork_url",  "")
            if art_path:
                ps.artwork_url = "file://" + art_path
            elif art_url:
                ps.artwork_url = self._blt_base + art_url

    # ── Phrase retry + cache helpers ──────────────────────────────────────────

    def _phrases_from_dicts(self, dicts: list) -> list:
        """Deserialiseer een lijst phrase-dicts naar Phrase objecten."""
        result = []
        for d in dicts:
            try:
                result.append(Phrase(
                    index      = int(d.get("index", 0)),
                    beat_start = int(d.get("beat_start", 0)),
                    beat_end   = int(d.get("beat_end", 0)),
                    kind       = str(d.get("kind", "")),
                    display    = str(d.get("display", "")),
                    fill       = bool(d.get("fill", False)),
                    fill_beat  = int(d.get("fill_beat", 0)),
                ))
            except Exception:
                pass
        return result

    def _retry_phrases_for_player(self, dev: int, track_id: int,
                                   max_retries: int = 10,
                                   interval_s: float = 0.5) -> None:
        """Achtergrondthread: poll tot phrases beschikbaar zijn of max_retries bereikt.

        Zodra live phrases binnenkomen via _handle_udp, stopt de loop vroeg.
        Als na max_retries de phrases nog steeds leeg zijn, laad dan de cache.
        """
        for attempt in range(1, max_retries + 1):
            time.sleep(interval_s)

            with self._lock:
                ps = self._players.get(dev)
                if ps is None or ps.track_id != track_id:
                    # Track gewisseld — stop retry
                    return
                if ps.phrases and ps.phrases_source == "live":
                    diag.info("P%d RETRY-OK attempt=%d phrases=%d [live]",
                              dev, attempt, len(ps.phrases))
                    return
                has_cache = self._meta_cache.has_phrases(track_id)
                still_empty = not ps.phrases

            diag.debug("P%d RETRY attempt=%d phrases=%s cache=%s",
                       dev, attempt, "empty" if still_empty else "partial", has_cache)

        # Max retries uitgeput — laad vanuit cache als beschikbaar
        cached = self._meta_cache.get_phrases(track_id)
        if cached:
            with self._lock:
                ps = self._players.get(dev)
                if ps and ps.track_id == track_id and not ps.phrases:
                    ps.phrases = self._phrases_from_dicts(cached)
                    ps.phrases_source = "cache"
                    diag.info("P%d RETRY-CACHE tid=%d phrases=%d (na %d×%.0fms)",
                              dev, track_id, len(cached), max_retries,
                              interval_s * 1000)
                    print(f"[P{dev}] Phrases loaded: [Source: Cache] "
                          f"({len(cached)} phrases, track {track_id})")
        else:
            diag.warning("P%d RETRY-FAIL tid=%d — geen live-phrases EN geen cache. "
                         "Fallback: phrase-heartbeat op BPM.", dev, track_id)
            with self._lock:
                ps = self._players.get(dev)
                if ps and ps.track_id == track_id:
                    ps.phrases_source = "fallback"

    # ── Diagnostics logger ────────────────────────────────────────────────────

    def _start_diagnostics_logger(self) -> None:
        """Log elke 15s een éénregelige statussamenvatting naar console + diag."""
        def _loop():
            while True:
                time.sleep(15.0)
                try:
                    self._log_status_summary()
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True, name="blt-diag").start()

    def _log_status_summary(self) -> None:
        """Print statusregel voor App Nap, phrases-bron en UDP-jitter."""
        from system_priority import SystemPriority as _SP
        # App Nap status — lees uit module-level singleton als die bestaat
        try:
            import sys
            sp_inst = getattr(sys.modules.get("__main__"), "_system_priority", None)
            app_nap_str = "YES" if (sp_inst and sp_inst._app_nap_ok) else "NO"
        except Exception:
            app_nap_str = "?"

        # Phrase bron per actieve speler
        phrase_sources = []
        now = time.time()
        with self._lock:
            for n, ps in sorted(self._players.items()):
                if ps.track_id > 0 and ps.is_playing:
                    src = ps.phrases_source or ("live" if ps.phrases else "—")
                    phrase_sources.append(f"P{n}:{src}")

        phrases_str = ", ".join(phrase_sources) if phrase_sources else "—"
        jitter_str  = f"{self._jitter.jitter_ms():.1f}ms"

        line = (f"App Nap disabled: {app_nap_str} | "
                f"Phrases loaded: [{phrases_str}] | "
                f"UDP Jitter: {jitter_str}")
        print(f"[BLT-STATUS] {line}")
        diag.info("DIAG %s", line)

    # ── HTTP params.json poller (fallback) ────────────────────────────────────

    def _start_http_poller(self):
        """Start HTTP poller - FALLBACK source + loop status"""
        def poll_loop():
            session = requests.Session()
            print(f"[BLT] HTTP polling {self._blt_base}/params.json")
            _conn_err_count = 0

            while True:
                try:
                    r = session.get(f"{self._blt_base}/params.json", timeout=1.0)
                    if r.status_code == 200:
                        _conn_err_count = 0
                        self._last_http = time.time()
                        self._handle_params(r.json())
                except Exception as e:
                    err_str = str(e)
                    # Suppress connection-refused spam (BLT not started yet)
                    if "Connection refused" in err_str or "Errno 61" in err_str:
                        _conn_err_count += 1
                        if _conn_err_count == 1:
                            print("[BLT HTTP] overlay server not ready, will retry silently")
                    elif time.time() - self._last_osc_beat > 5:
                        print(f"[BLT HTTP] {e}")

                time.sleep(1.0)  # 1s poll

        threading.Thread(target=poll_loop, daemon=True, name="blt-http").start()

    def _handle_params(self, data: dict):
        """
        Parse BLT params.json format.
        BLT uses hyphenated keys and a nested track:{} object.

        Authority:
        - beat_number, bpm, on_air, is_master, is_synced: written here only if
          OSC hasn't fired recently (last_beat_time age > 2s)
        - track_id, title, artist, duration_ms, key, genre: always updated
        - is_looping: always (only source)
        """
        for num_str, p in data.get("players", {}).items():
            try:
                num = int(num_str)

                with self._lock:
                    ps = self._players.get(num)
                    if not ps:
                        continue

                    if not p.get("is-track-loaded", False):
                        continue

                    track = p.get("track") or {}

                    # Track identity — HTTP fills in if UDP/OSC haven't set it yet
                    tid = int(track.get("id", 0) or 0)
                    if tid > 0 and ps.track_id == 0:
                        ps.track_id = tid
                    # Only update track metadata if HTTP track ID matches ours
                    # Require tid > 0 to match — tid==0 means params.json has no track data,
                    # don't let that overwrite fresh UDP metadata with empty strings.
                    http_matches = tid > 0 and (tid == ps.track_id or ps.track_id == 0)
                    if http_matches:
                        v = str(track.get("title",  "") or "")
                        if v: ps.title  = v
                        v = str(track.get("artist", "") or "")
                        if v: ps.artist = v
                        v = str(track.get("key",    "") or "")
                        if v: ps.key    = v
                        v = str(track.get("genre",  "") or "")
                        if v: ps.genre  = v
                        v = str(track.get("comment","") or "")[:60]
                        if v: ps.mood   = v

                    # Track structure — HTTP fills in if UDP hasn't set it yet
                    dur_s = float(track.get("duration", 0) or 0)
                    if dur_s > 0 and ps.duration_ms == 0:
                        ps.duration_ms = dur_s * 1000.0

                    track_bpm = float(p.get("track-bpm", 0) or 0)
                    if track_bpm > 0 and ps.track_bpm == 0:
                        ps.track_bpm = track_bpm

                    # Loop status — HTTP is authoritative (only reliable source)
                    ps.is_looping = bool(p.get("is-looping", False))

                    # Real-time status — write only if OSC is stale
                    osc_age = (
                        time.time() - ps.last_beat_time
                        if ps.last_beat_time > 0 else 999
                    )
                    if osc_age > 2.0:
                        beat_n = int(p.get("beat-number", 0) or 0)
                        if beat_n > 0:
                            ps.beat_number     = beat_n
                            ps.beat_within_bar = int(p.get("beat-within-bar", 0) or 0)
                        ps.bpm        = float(p.get("tempo", 0) or 0)
                        ps.is_playing = bool(p.get("is-playing", False))
                        ps.on_air     = bool(p.get("is-on-air", False))
                        ps.is_master  = bool(p.get("is-tempo-master", False))
                        ps.is_synced  = bool(p.get("is-synced", False))

            except Exception as e:
                print(f"[BLT HTTP] player {num_str}: {e}")

    # ── State file poller ─────────────────────────────────────────────────────

    def _start_state_poller(self):
        """Poll /tmp/vp_state.json written by BLT vp-beat-entry! on every beat + 1.5s re-dump"""
        import os

        def poll_loop():
            state_path = "/tmp/vp_state.json"
            last_mtime = 0.0
            print(f"[BLT] State file polling {state_path}")

            while True:
                try:
                    mtime = os.path.getmtime(state_path)
                    if mtime != last_mtime:
                        last_mtime = mtime
                        with open(state_path, "r") as f:
                            data = json.load(f)
                        self._handle_state(data)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
                time.sleep(0.1)

        threading.Thread(target=poll_loop, daemon=True, name="blt-state").start()

    def _handle_state(self, data: dict):
        """Parse /tmp/vp_state.json — authoritative for phrases and beat timing from BLT"""
        self._last_state = time.time()
        for num_str, p in data.get("players", {}).items():
            try:
                num = int(num_str)

                phrases_data = p.get("phrases", [])
                beat_n       = int(p.get("beat_number", 0) or 0)
                bpm          = float(p.get("bpm", 0) or 0)

                # Skip players with no useful data at all
                if not phrases_data and beat_n <= 0 and bpm <= 0:
                    continue

                with self._lock:
                    ps = self._players.get(num)
                    if not ps:
                        continue

                    state_tid = int(p.get("track_id", 0) or 0)
                    if state_tid > 0:
                        if ps.track_id == 0:
                            # First time we know what track is loaded — accept it
                            ps.track_id = state_tid
                        elif state_tid != ps.track_id:
                            # State file still has data from previous track — skip everything
                            diag.info("P%d STATE-SKIP stale tid=%d (current=%d)",
                                      num, state_tid, ps.track_id)
                            continue
                    elif state_tid < 0:
                        # BLT signals no track loaded
                        ps.track_id        = 0
                        ps.title           = ""
                        ps.artist          = ""
                        ps.osc_phrase      = None
                        ps.phrase_kind_blt = ""
                        continue

                    # ── State file data is for our current track from here ──

                    # Beat data: defer to OSC if fresh, otherwise take from state file
                    osc_beat_age = (time.time() - ps.last_beat_time
                                    if ps.last_beat_time > 0 else 999)
                    if osc_beat_age > 1.0:
                        if beat_n > 0:
                            ps.beat_number = beat_n
                        if bpm > 0:
                            ps.bpm = bpm
                        ps.on_air     = bool(p.get("on_air",     False))
                        ps.is_playing = bool(p.get("is_playing", False))
                        ps.is_master  = bool(p.get("is_master",  False))

                    # Update beat anchor from state file position so dead-reckoning
                    # stays accurate even when OSC beats are stale or after track change.
                    pos_ms = float(p.get("position_ms", 0) or 0)
                    if pos_ms > 0 and bpm > 0:
                        now_t = time.time()
                        if ps.beat_anchor_time > 0 and ps.beat_anchor_bpm > 0:
                            _beat_dur = 60000.0 / ps.beat_anchor_bpm
                            _elapsed  = (now_t - ps.beat_anchor_time) * 1000.0
                            _dr_pos   = ps.beat_anchor_pos_ms + min(_elapsed, _beat_dur * 30.0)
                        else:
                            _dr_pos = ps.beat_anchor_pos_ms
                        diff = pos_ms - _dr_pos
                        if abs(diff) > 50.0 or ps.beat_anchor_time <= 0:
                            if abs(diff) > 500.0 or ps.beat_anchor_time <= 0:
                                diag.info("P%d STATE-ANCHOR pos=%.0fms dr=%.0fms diff=%.0fms bn=%d bpm=%.1f",
                                          num, pos_ms, _dr_pos, diff, beat_n, bpm)
                            ps.beat_anchor_pos_ms = pos_ms
                            ps.beat_anchor_time   = now_t
                            ps.beat_anchor_bpm    = bpm

                    # BLT's authoritative phrase
                    pk = str(p.get("phrase_kind", "") or "")
                    if pk:
                        ps.phrase_kind_blt = pk
                    ps.beats_to_next_blt = int(p.get("beats_to_next", 0) or 0)

                    # Phrases — only accept if state file track matches current track
                    if phrases_data:
                        phrases = []
                        for ph in phrases_data:
                            try:
                                phrases.append(Phrase(
                                    index      = int(ph.get("index",      0)),
                                    beat_start = int(ph.get("beat_start", 0)),
                                    beat_end   = int(ph.get("beat_end",   0)),
                                    kind       = str(ph.get("kind",       "")),
                                    display    = str(ph.get("display",    "")),
                                    fill       = bool(ph.get("fill",      False)),
                                    fill_beat  = int(ph.get("fill_beat",  0)),
                                ))
                            except Exception:
                                pass
                        if phrases:
                            ps.phrases = phrases
                            diag.info("P%d STATE-PHRASES count=%d first=%s last=%s",
                                      num, len(phrases),
                                      phrases[0].kind if phrases else "-",
                                      phrases[-1].kind if phrases else "-")

            except Exception:
                pass

    # ── Snapshot API ──────────────────────────────────────────────────────────

    def get_snapshot(self, player_num: int) -> PlayerSnapshot:
        """Thread-safe immutable snapshot"""
        with self._lock:
            ps = self._players.get(player_num)
            if not ps:
                return self._empty_snapshot(player_num)

            # Current phrase: beat-scan against ps.phrases is most reliable.
            # beat_number is rock-solid (CDJ encoder); phrases come from rekordbox via UDP.
            # Use phrase_kind_blt only as a tiebreaker when multiple phrases of same type exist.
            bn = ps.beat_number
            if ps.phrases and bn > 0:
                current_phrase = self._find_phrase_at_beat(ps.phrases, bn)
                # If phrase_kind_blt disagrees with beat-scan result, log it (debug)
            elif ps.osc_phrase:
                op = ps.osc_phrase
                current_phrase = op if (op.beat_end == 0 or bn < op.beat_end) else None
            else:
                current_phrase = None

            # beats_to_next: use BLT's value if fresh, else compute
            beats_to_next = (ps.beats_to_next_blt if ps.beats_to_next_blt > 0
                             else self._beats_to_next_phrase(ps.phrases, ps.beat_number))

            # Capture values needed for position calculation (outside lock)
            bpm_now        = ps.bpm or ps.track_bpm
            anchor_pos_ms  = ps.beat_anchor_pos_ms
            anchor_time    = ps.beat_anchor_time
            anchor_bpm     = ps.beat_anchor_bpm or bpm_now
            dur            = ps.duration_ms
            is_playing     = ps.is_playing
            is_looping     = ps.is_looping

            snap_kwargs = dict(
                player               = ps.player,
                track_id             = ps.track_id,
                title                = ps.title,
                artist               = ps.artist,
                beat_number          = ps.beat_number,
                beat_within_bar      = ps.beat_within_bar,
                is_playing           = is_playing,
                bpm                  = ps.bpm,
                track_bpm            = ps.track_bpm,
                duration_ms          = dur,
                phrases              = tuple(ps.phrases),
                cues                 = tuple(ps.cues),
                current_phrase       = current_phrase,
                beats_to_next_phrase = beats_to_next,
                on_air               = ps.on_air,
                is_master            = ps.is_master,
                is_synced            = ps.is_synced,
                is_looping           = ps.is_looping,
                key                  = ps.key,
                genre                = ps.genre,
                mood                 = ps.mood,
                waveform_url         = ps.waveform_url,
                artwork_url          = ps.artwork_url,
                bass_energy          = ps.bass_energy,
                mid_energy           = ps.mid_energy,
                treble_energy        = ps.treble_energy,
            )
        # ── Position dead-reckoning from beat anchor (outside lock) ──────────────
        # Beat numbers from /blt/beat are rock solid (CDJ platter encoder).
        # anchor_pos_ms = (beat_n - 1) * beat_dur  — exact position when beat fired.
        # We dead-reckon forward at BPM speed since then.
        now = time.time()
        osc_beat_age = (now - self._last_osc_beat) if self._last_osc_beat > 0 else 999.0

        if not is_playing or anchor_time <= 0:
            position_ms = anchor_pos_ms
        else:
            beat_dur_ms = (60000.0 / anchor_bpm) if anchor_bpm > 0 else 480.0
            elapsed_ms  = (now - anchor_time) * 1000.0
            # Loop: cap just under one beat to avoid overshooting the loop point.
            # Normal: cap at 30 beats — covers frozen-trigger gaps up to ~15s at 120bpm
            # so the watchdog (fires every 5s) always has time to kick in first.
            cap = beat_dur_ms * 0.95 if is_looping else beat_dur_ms * 30.0
            elapsed_ms  = min(elapsed_ms, cap)
            position_ms = anchor_pos_ms + elapsed_ms
            if dur > 0:
                position_ms = min(position_ms, dur)
            position_ms = max(position_ms, 0.0)

        # ── Phrase heartbeat: synthesise beat_number when OSC is stale ───────────
        # When BLT stops sending beats (network hiccup / App Nap / freeze), the
        # last known beat_number is stale.  Derive a synthetic beat from the
        # dead-reckoned position so the phrase cursor keeps advancing.
        # Only applied when: OSC stale >1.5s AND we have an anchor BPM AND
        #                     phrases are loaded (heartbeat only advances within them).
        beat_number_for_phrase = snap_kwargs["beat_number"]
        phrases_for_phrase     = snap_kwargs["phrases"]
        if (osc_beat_age > 1.5
                and anchor_bpm > 0
                and anchor_pos_ms > 0
                and phrases_for_phrase):
            beat_dur_ms_hb  = 60000.0 / anchor_bpm
            synth_beat_float = position_ms / beat_dur_ms_hb
            synth_beat       = max(1, int(synth_beat_float) + 1)
            # Use synthesised beat only when it's ahead of (or equal to) the
            # stale OSC beat — never go backwards.
            if synth_beat >= beat_number_for_phrase:
                beat_number_for_phrase = synth_beat
                # Re-derive current_phrase and beats_to_next from the heartbeat beat
                snap_kwargs["beat_number"] = synth_beat
                snap_kwargs["current_phrase"] = self._find_phrase_at_beat(
                    list(phrases_for_phrase), synth_beat)
                snap_kwargs["beats_to_next_phrase"] = self._beats_to_next_phrase(
                    list(phrases_for_phrase), synth_beat)

        return PlayerSnapshot(
            position_ms  = position_ms,
            **snap_kwargs,
        )

    def _find_phrase_by_kind_at_beat(self, phrases, kind: str, beat_num: int):
        """Find the Phrase whose kind matches and whose range contains beat_num.
        When multiple phrases share the same kind, pick the one active at beat_num.
        Falls back to any phrase with matching kind if none covers the beat exactly."""
        if not phrases or not kind:
            return None
        best = None
        for p in phrases:
            if p.kind != kind:
                continue
            in_range = (p.beat_start <= beat_num and
                        (p.beat_end == 0 or beat_num < p.beat_end))
            if in_range:
                return p          # exact match — return immediately
            if best is None:
                best = p          # fallback: first phrase with matching kind
        return best

    def _find_phrase_at_beat(self, phrases, beat_num):
        """Find phrase containing beat_num (no stabilization)"""
        if not phrases or beat_num <= 0:
            return None

        active = None
        for p in sorted(phrases, key=lambda x: x.beat_start):
            if p.beat_start <= beat_num:
                if p.beat_end == 0 or beat_num < p.beat_end:
                    active = p
            else:
                break
        return active

    def _find_phrase_at_beat_stable(self, ps: _PlayerState, beat_num: int):
        """
        Find phrase at beat_num with hysteresis to prevent jitter.

        Only change phrase if:
        1. We're actually at a phrase boundary (beat_start), OR
        2. The locked phrase has ended
        """
        if not ps.phrases or beat_num <= 0:
            ps.last_phrase_index      = -1
            ps.phrase_lock_until_beat = 0
            return None

        # If we have a locked phrase and haven't reached unlock beat, keep it
        # BUT only if beat hasn't jumped backwards (loop/seek)
        if ps.last_phrase_index >= 0 and beat_num < ps.phrase_lock_until_beat:
            for p in ps.phrases:
                if p.index == ps.last_phrase_index:
                    if p.beat_start <= beat_num:   # still inside or after phrase start
                        return p
                    break                          # jumped back — fall through to find new phrase

        # Find the actual current phrase
        active = None
        for p in sorted(ps.phrases, key=lambda x: x.beat_start):
            if p.beat_start <= beat_num:
                if p.beat_end == 0 or beat_num < p.beat_end:
                    active = p
            else:
                break

        # Lock new phrase until its end
        if active and active.index != ps.last_phrase_index:
            ps.last_phrase_index      = active.index
            ps.phrase_lock_until_beat = (
                active.beat_end if active.beat_end > 0 else beat_num + 32
            )
            diag.debug("P%d phrase → %s (lock until beat %d)",
                       ps.player, active.display, ps.phrase_lock_until_beat)

        return active

    def _beats_to_next_phrase(self, phrases, beat_num):
        """Beats until next phrase starts"""
        if not phrases or beat_num <= 0:
            return 0

        for p in sorted(phrases, key=lambda x: x.beat_start):
            if p.beat_start > beat_num:
                return p.beat_start - beat_num
        return 0

    def _empty_snapshot(self, player_num):
        """Return empty snapshot for player with no track"""
        return PlayerSnapshot(
            player=player_num, track_id=0, title="", artist="",
            beat_number=0, beat_within_bar=0, is_playing=False,
            bpm=0.0, position_ms=0.0, track_bpm=0.0, duration_ms=0.0,
            phrases=(), cues=(), current_phrase=None, beats_to_next_phrase=0,
            on_air=False, is_master=False, is_synced=False, is_looping=False,
            key="", genre="", mood="", waveform_url="", artwork_url="",
        )

    def get_udp_jitter_ms(self) -> float:
        """Geef de huidige UDP jitter in milliseconden (rolling 16-sample window)."""
        return self._jitter.jitter_ms()

    def get_health(self) -> dict:
        """Returns data source health status for UI display"""
        now = time.time()

        def status_for_age(age, threshold):
            return "ok" if age < threshold else "stale"

        osc_age   = now - self._last_osc_beat if self._last_osc_beat > 0 else 999
        udp_age   = now - self._last_udp      if self._last_udp      > 0 else 999
        http_age  = now - self._last_http     if self._last_http     > 0 else 999
        state_age = now - self._last_state    if self._last_state    > 0 else 999

        return {
            "osc": {
                "last_update": self._last_osc_beat,
                "age":    osc_age,
                "status": status_for_age(osc_age, 3.0),
            },
            "udp": {
                "last_update": self._last_udp,
                "age":    udp_age,
                "status": status_for_age(udp_age, 30.0),
                "jitter_ms": self._jitter.jitter_ms(),
            },
            "http": {
                "last_update": self._last_http,
                "age":    http_age,
                "status": status_for_age(http_age, 2.0),
            },
            "state": {
                "last_update": self._last_state,
                "age":    state_age,
                "status": status_for_age(state_age, 30.0),
            },
        }

    def nudge_stuck_players(self, snaps: dict) -> None:
        """Called every 5s — if a player is playing but last_beat_time is stale >8s,
        the BLT trigger has frozen. Force-call vp-beat-fn! via nREPL to unstick it."""
        import socket as _sock
        now = time.time()
        for n, snap in snaps.items():
            if not (snap.has_track and snap.is_playing and snap.bpm > 0):
                continue
            with self._lock:
                ps = self._players.get(n)
                if ps is None:
                    continue
                beat_age  = now - ps.last_beat_time  if ps.last_beat_time  > 0 else 999
                jump_age  = now - ps.last_jump_time  if ps.last_jump_time  > 0 else 999
            # Suppress nudge for 3s after a beat/cue jump — CDJ is seeking, beats will resume
            if beat_age > 1.5 and jump_age > 3.0:
                try:
                    def be(txt):
                        b = txt.encode('utf-8')
                        return f'{len(b)}:'.encode() + b
                    code = f"""(in-ns 'beat-link-trigger.expressions.triggers)
(let [vcj (org.deepsymmetry.beatlink.VirtualCdj/getInstance)
      s   (or (.getLatestStatusFor vcj (int {n}))
              ;; Fallback: scan all cached statuses for matching device
              (first (filter #(= (int (.getDeviceNumber %)) (int {n}))
                             (vals (.getLatestStatuses vcj)))))
      f   (:vp-beat-fn! @globals)]
  (when (and s f) (f s s false) :ok))"""
                    sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                    sock.connect(('127.0.0.1', 17001))
                    sock.settimeout(3)
                    msg = b'd' + be('op') + be('eval') + be('code') + be(code) + be('id') + be('w') + b'e'
                    sock.sendall(msg)
                    time.sleep(0.2)
                    sock.close()
                    print(f"[watchdog] nudged stuck player {n} (beat stale {beat_age:.0f}s)")
                except Exception as e:
                    print(f"[watchdog] nudge P{n} failed: {e}")
