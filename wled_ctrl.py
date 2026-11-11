"""
djlights — unified VJ app
Two tabs:
  CDJ MONITOR  — live Pro DJ Link status, auto LED engine
  LED CONTROL  — manual effect controller with geometry canvas
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time, threading, math, logging, random, tkinter as tk, json
from datetime import datetime
from dataclasses import dataclass, field, asdict
import customtkinter as ctk
import numpy as np
from PIL import Image, ImageTk

from device_manager  import DeviceManager, Device
from palette_manager import PaletteManager
from udp_handler     import DDPSender
from geometry        import Geometry, SnakeGeometry, LinearGeometry, GlobeGeometry, rainbow_preview
from effects         import CATEGORIES
from effects.chorus  import ChorusImpactFlash

# scipy — used only for preview post-processing (UI thread), never in DDP hot path.
# Imported at module load so both UI thread and render thread share the same
# already-loaded module and never race on the import lock.
try:
    from scipy.ndimage    import zoom              as _scipy_zoom
    from scipy.ndimage    import gaussian_filter1d as _scipy_gf1d
    from scipy.ndimage    import gaussian_filter   as _scipy_gf2d
    from scipy.interpolate import interp1d         as _scipy_interp1d
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False

# Pre-computed Gaussian kernel for _spatial_bloom — pure numpy, zero scipy in
# the render hot path.  sigma=1.8 ≈ 3 LED radius at 60 LED/m density.
_BLOOM_SIGMA  = 1.8
_bloom_r      = int(3.0 * _BLOOM_SIGMA + 0.5)          # 5
_bloom_x      = np.arange(-_bloom_r, _bloom_r + 1, dtype=np.float32)
_BLOOM_KERNEL = np.exp(-_bloom_x ** 2 / (2.0 * _BLOOM_SIGMA ** 2))
_BLOOM_KERNEL /= _BLOOM_KERNEL.sum()                    # normalise

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
_log = logging.getLogger("djlights")

# File logger — captures DEBUG+ from all loggers into a timestamped file
_app_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             f"djlights_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
_file_handler = logging.FileHandler(_app_log_path, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(name)s: %(message)s",
    datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.DEBUG)
# Keep console at WARNING to avoid noise on screen
logging.getLogger().handlers[0].setLevel(logging.WARNING)

# Optional ProDJ Link / engine
try:
    from prodj.core.prodj import ProDj as _ProDj
    import engine  as _engine
    import wled    as _wled_auto
    PRODJ_OK = True
except Exception as _e:
    _log.warning("ProDJ Link not available: %s", _e)
    PRODJ_OK = False

# BLT client
try:
    from blt_client  import BLTClient
    from player_state import PlayerSnapshot, Phrase, CuePoint
    BLT_OK = True
except Exception as _be:
    _log.warning("BLT client not available: %s", _be)
    BLT_OK = False


class _BLTEngineAdapter:
    """Adapts a PlayerSnapshot to the interface expected by engine.py."""

    def __init__(self, snap):
        self._snap = snap
        # Build _pssi from snap.phrases so engine._current_phrase_for() works
        if snap.phrases:
            entries = [{"beat": p.beat_start, "kind_name": _phrase_base(p.kind),
                        "fill": p.fill, "beat_fill": p.fill_beat}
                       for p in snap.phrases]
            self._pssi = {"mood": 0, "end_beat": snap.phrases[-1].beat_end, "entries": entries}
        else:
            self._pssi = None
        self._beatgrid = None

    # Engine-required attributes
    @property
    def bpm(self):             return self._snap.bpm
    @property
    def beat(self):            return self._snap.beat_within_bar   # 1-4
    @property
    def beat_count(self):      return self._snap.beat_number       # absolute beat in track
    @property
    def position(self):        return self._snap.position_ms / 1000.0
    @property
    def is_playing(self):      return self._snap.is_playing
    @property
    def play_state(self):      return "playing" if self._snap.is_playing else "paused"
    @property
    def on_air(self):          return self._snap.on_air
    @property
    def player_number(self):   return self._snap.player
    # Waveform lookup key
    @property
    def loaded_player_number(self): return self._snap.player
    @property
    def loaded_slot(self):     return "usb"
    @property
    def track_id(self):        return self._snap.track_id


# ── Theme ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

BG, BG2, BG3, BG4  = "#0a0a0a", "#111111", "#1a1a1a", "#222222"
BORDER   = "#2a2a2a"
ACCENT   = "#00cc88"
ACCENT2  = "#0077dd"
TEXT     = "#ffffff"
DIM      = "#777777"
KILL_COL = "#cc2233"
_FF      = "Helvetica Neue"
MONO9    = (_FF, 9, "bold")
MONO10   = (_FF, 10, "bold")

PHRASE_COLORS = {
    "intro":  "#2d88f5", "verse": "#2dd45e", "up":    "#f5b830",
    "chorus": "#f53030", "bridge":"#b830f5", "outro": "#4d88cc",
    "down":   "#4d88cc", "":      "#444444",
}

# Default brightness applied in RANDOM mode (when AI hasn't decided)
PHRASE_BRIGHTNESS = {
    "intro":  0.35,
    "verse":  0.60,
    "up":     0.80,
    "chorus": 1.00,
    "bridge": 0.55,
    "down":   0.45,
    "outro":  0.30,
}

# Palette pools per phrase type — names must match entries in palettes.json.
# Selection picks randomly within the pool, avoiding the currently active palette.
# Warm palettes (Amber, Velvet, Ember) are preferred for small-venue warmth.
_ALL_PALETTES = [
    "Cyberpunk", "Fire", "Ocean", "Sunset", "Neon", "beam", "bro",
    "DeepHouse", "BlueOnly", "Contrast", "Amber", "DeepBlue",
    "Violet", "Crimson", "Velvet", "Monochrome",
    "Orange", "Purple", "DarkRed", "Pink",
]
PHRASE_PALETTE_POOLS = {
    # Cool, restrained — set the mood without competing with the drop
    "intro":  ["DeepBlue", "Ocean", "Violet", "Purple", "BlueOnly", "Monochrome"],
    # Groove — medium energy, varied colour
    "verse":  ["Neon", "DeepHouse", "Ocean", "Sunset", "beam", "bro", "Violet", "Purple", "Pink"],
    # Tension building — warm colours escalating toward peak
    "up":     ["Sunset", "Neon", "Cyberpunk", "Contrast", "Amber", "Fire", "Orange", "bro"],
    # Peak impact — hottest, most saturated palettes
    "chorus": ["Fire", "Cyberpunk", "Crimson", "DarkRed", "Contrast", "Neon", "Amber", "Orange", "Sunset"],
    # Release, breathe — cool and atmospheric
    "down":   ["DeepBlue", "Ocean", "Violet", "Purple", "Velvet", "BlueOnly", "DeepHouse"],
    # Mysterious, transitional — unusual palette pairings
    "bridge": ["Purple", "Pink", "Velvet", "Cyberpunk", "DarkRed", "Monochrome", "Crimson"],
    # Fade out — sparse and subdued
    "outro":  ["DeepBlue", "BlueOnly", "Monochrome", "Violet", "Purple", "Ocean", "Velvet"],
}

# Default spacing when AI is not active or random path is used.
# Never 0 except chorus — forces visual variety.
PHRASE_SPACING_DEFAULT = {
    "intro":  18,
    "verse":  10,
    "up":     7,
    "chorus": 4,
    "bridge": 12,
    "down":   10,
    "outro":  18,
}

# Phrase aliases — map BLT phrase kinds to effect categories
PHRASE_ALIAS = {"bridge": "down"}

# Default direction per phrase — applied in random path and as AI fallback.
PHRASE_DIRECTION_DEFAULT = {
    "intro":  "forward",
    "verse":  "forward",
    "up":     "forward",
    "chorus": "mirror",
    "bridge": "reverse",
    "down":   "reverse",
    "outro":  "forward",
}

# ── User settings (settings.json, edited in the SETTINGS tab) ────────────────
import settings as _settings


def apply_phrase_settings():
    """Load phrase brightness + palette pools from settings.json into the
    module-level dicts *in place*, so the render loop picks them up live."""
    _b = _settings.get("phrases.brightness", {}) or {}
    for _k, _v in _b.items():
        try:
            PHRASE_BRIGHTNESS[_k] = max(0.0, min(1.0, float(_v)))
        except (TypeError, ValueError):
            pass
    _p = _settings.get("phrases.palette_pools", {}) or {}
    for _k, _v in _p.items():
        if isinstance(_v, list) and _v:
            PHRASE_PALETTE_POOLS[_k] = [str(x) for x in _v]


apply_phrase_settings()

def _phrase_base(kind: str) -> str:
    """BLT uses 'up_1', 'chorus_1' etc. Strip numeric suffix for colour lookup."""
    if not kind:
        return ""
    if kind in PHRASE_COLORS:
        return kind
    # strip trailing _N
    base = kind.rsplit("_", 1)[0]
    return base if base in PHRASE_COLORS else kind

def _phrase_color(kind: str) -> str:
    return PHRASE_COLORS.get(_phrase_base(kind), PHRASE_COLORS[""])

KEY_NAMES = {
    0:"—", 1:"Am 1A", 2:"C 1B", 3:"Em 2A", 4:"G 2B", 5:"Bm 3A",
    6:"D 3B", 7:"F#m 4A", 8:"A 4B", 9:"C#m 5A", 10:"E 5B",
    11:"G#m 6A", 12:"B 6B", 13:"Ebm 7A", 14:"F# 7B", 15:"Bbm 8A",
    16:"Db 8B", 17:"Fm 9A", 18:"Ab 9B", 19:"Cm 10A", 20:"Eb 10B",
    21:"Gm 11A", 22:"Bb 11B", 23:"Dm 12A", 24:"F 12B",
}


def _spatial_bloom(buf: np.ndarray, threshold: int = 128) -> np.ndarray:
    """Global spatial bloom — applied to every frame before DDP send and preview store.

    Steps:
      1. Threshold  — soft-extract hotspots: pixels with value >= threshold
      2. 1D Gaussian blur  — spread the isolated hotspot light (pre-computed kernel)
      3. Additive composite — add glow back to original, clamp to uint8

    Operates on (n, 3) uint8.  Returns (n, 3) uint8.  Pure numpy — no scipy in hot path.
    """
    f   = buf.astype(np.float32)
    hot = np.where(f >= threshold, f - threshold, 0.0)
    glow = np.stack(
        [np.convolve(hot[:, ch], _BLOOM_KERNEL, mode="same") for ch in range(3)],
        axis=1,
    )
    return (f + glow).clip(0.0, 255.0).astype(np.uint8)


def _process_strip_preview(px: np.ndarray, target_w: int) -> np.ndarray:
    """Preview post-processing wrapper — bloom is already baked into px by _spatial_bloom.

    Stages (float32 [0..255]):
      1. Optical diffusion — gaussian_filter1d sigma=1.0 (LED-space, simulates diffuser)
      2. Cubic upsample  — scipy zoom order=3 for fluid sub-pixel motion
      3. Inverse gamma   — (val/255)^(1/2.2)*255  makes WS2812B output perceptually correct

    Returns (target_w, 3) uint8.
    """
    if not _SCIPY_OK:
        # Fallback: nearest-neighbour upsample, no blur
        n = len(px)
        idx = (np.linspace(0, n - 1, target_w)).astype(np.int32).clip(0, n - 1)
        return px[idx].astype(np.uint8)

    n = len(px)
    if n == 0 or target_w < 1:
        return np.zeros((max(1, target_w), 3), dtype=np.uint8)

    buf = px.astype(np.float32)

    # 1. Optical diffusion in LED-space
    for ch in range(3):
        buf[:, ch] = _scipy_gf1d(buf[:, ch], sigma=1.0, mode="nearest")

    # 2. Cubic upsample to canvas width
    if target_w != n:
        scale = target_w / n
        up = np.stack(
            [_scipy_zoom(buf[:, ch], scale, order=3) for ch in range(3)],
            axis=1,
        )
        if up.shape[0] != target_w:
            xs_src = np.linspace(0, 1, up.shape[0])
            xs_dst = np.linspace(0, 1, target_w)
            up = np.stack(
                [_scipy_interp1d(xs_src, up[:, ch], kind="cubic")(xs_dst)
                 for ch in range(3)],
                axis=1,
            )
    else:
        up = buf

    # 3. Inverse gamma: physical linear → perceptual display
    up = np.power(np.clip(up / 255.0, 0.0, 1.0), 1.0 / 2.2) * 255.0

    return up.clip(0.0, 255.0).astype(np.uint8)


def _draw_px_strip(canvas: tk.Canvas, px: np.ndarray, height: int) -> None:
    """Render a pixel array onto a tk.Canvas — gamma + diffuser + bilinear + bloom."""
    w = canvas.winfo_width()
    if w < 2 or px is None or len(px) == 0:
        return
    row = _process_strip_preview(px, w)                    # (w, 3)
    arr = np.broadcast_to(row[np.newaxis], (height, w, 3)).copy()
    photo = ImageTk.PhotoImage(Image.fromarray(arr))
    canvas.delete("all")
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas._px_photo = photo                               # prevent GC


def _phrase_for_snap(snap) -> str:
    """Return display-friendly phrase name (uses Phrase.display when available)."""
    if snap is None:
        return ""
    cp = getattr(snap, "current_phrase", None)
    if not cp:
        return ""
    # Prefer the rekordbox display name ("Verse 1", "Chorus 1") over raw kind
    return cp.display if cp.display else _phrase_base(cp.kind)

def _score_snap(s):
    return (s.is_master, s.on_air, s.is_playing, s.bpm > 0, round(s.position_ms))


# ══════════════════════════════════════════════════════════════════════════════
# CDJ Monitor tab
# ══════════════════════════════════════════════════════════════════════════════

class PlayerCard(ctk.CTkFrame):
    def __init__(self, master, player_num, **kw):
        super().__init__(master, fg_color=BG2, corner_radius=6, **kw)
        self._pnum       = player_num
        self._meta_cache = None   # last known metadata dict
        self._tid_cache  = None   # track_id of cached metadata
        self._build()

    def _build(self):
        # ── Header row ────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=6, pady=(5, 0))
        self._pnum_lbl = ctk.CTkLabel(hdr, text=f"P{self._pnum}",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=DIM)
        self._pnum_lbl.pack(side="left")
        self._onair_lbl = ctk.CTkLabel(hdr, text=" ", width=18,
            font=ctk.CTkFont(size=10), text_color=KILL_COL)
        self._onair_lbl.pack(side="left", padx=2)
        # MASTER / SYNC / LOOP badges (right side of header)
        self._loop_lbl = ctk.CTkLabel(hdr, text="", width=36,
            font=ctk.CTkFont(size=8, weight="bold"), text_color=DIM)
        self._loop_lbl.pack(side="right", padx=1)
        self._sync_lbl = ctk.CTkLabel(hdr, text="", width=40,
            font=ctk.CTkFont(size=8, weight="bold"), text_color=DIM)
        self._sync_lbl.pack(side="right", padx=1)
        self._master_lbl = ctk.CTkLabel(hdr, text="", width=46,
            font=ctk.CTkFont(size=8, weight="bold"), text_color=DIM)
        self._master_lbl.pack(side="right", padx=1)

        # ── Track ─────────────────────────────────────────────────────────────
        self._title_lbl = ctk.CTkLabel(self, text="—",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=DIM, anchor="w", wraplength=150)
        self._title_lbl.pack(fill="x", padx=6, pady=(2, 0))
        self._artist_lbl = ctk.CTkLabel(self, text="",
            font=("Helvetica Neue", 9), text_color=DIM, anchor="w")
        self._artist_lbl.pack(fill="x", padx=6)

        # ── BPM + Key ─────────────────────────────────────────────────────────
        bk = ctk.CTkFrame(self, fg_color="transparent")
        bk.pack(fill="x", padx=6, pady=(4, 0))
        ctk.CTkLabel(bk, text="BPM", font=("Helvetica Neue", 8),
                     text_color=DIM).pack(side="left")
        self._bpm_lbl = ctk.CTkLabel(bk, text="—", width=52,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=DIM)
        self._bpm_lbl.pack(side="left", padx=(2, 4))
        self._pitch_lbl = ctk.CTkLabel(bk, text="", width=36,
            font=("Helvetica Neue", 8), text_color=DIM)
        self._pitch_lbl.pack(side="left", padx=(0, 4))
        ctk.CTkLabel(bk, text="KEY", font=("Helvetica Neue", 8),
                     text_color=DIM).pack(side="left")
        self._key_lbl = ctk.CTkLabel(bk, text="—",
            font=("Helvetica Neue", 9), text_color=DIM)
        self._key_lbl.pack(side="left", padx=2)

        # ── Beat dots + phrase ────────────────────────────────────────────────
        bd = ctk.CTkFrame(self, fg_color="transparent")
        bd.pack(fill="x", padx=6, pady=(3, 0))
        self._beat_dots = []
        for _ in range(4):
            d = ctk.CTkLabel(bd, text="○",
                             font=ctk.CTkFont(size=13), text_color=DIM)
            d.pack(side="left", padx=1)
            self._beat_dots.append(d)
        self._phrase_lbl = ctk.CTkLabel(bd, text="",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=DIM)
        self._phrase_lbl.pack(side="right", padx=4)

        # ── Position bar ──────────────────────────────────────────────────────
        self._pos_bar = ctk.CTkProgressBar(self, height=5, corner_radius=2,
                                            progress_color=ACCENT2, fg_color=BORDER)
        self._pos_bar.pack(fill="x", padx=6, pady=(4, 6))
        self._pos_bar.set(0)

    def update(self, snap=None, client=None):
        if snap is None or not snap.has_track:
            self._pnum_lbl.configure(text_color=DIM)
            self._onair_lbl.configure(text=" ")
            self._master_lbl.configure(text="", text_color=DIM)
            self._sync_lbl.configure(text="", text_color=DIM)
            self._loop_lbl.configure(text="", text_color=DIM)
            self._title_lbl.configure(text="—", text_color=DIM)
            self._artist_lbl.configure(text="no track" if snap else "")
            self._bpm_lbl.configure(text="—", text_color=DIM)
            self._pitch_lbl.configure(text="", text_color=DIM)
            self._key_lbl.configure(text="—", text_color=DIM)
            for d in self._beat_dots:
                d.configure(text="○", text_color=DIM)
            self._phrase_lbl.configure(text="", text_color=DIM)
            self._pos_bar.set(0)
            return

        is_playing = snap.is_playing
        on_air     = snap.on_air
        pc         = KILL_COL if on_air else (ACCENT if is_playing else DIM)

        self._pnum_lbl.configure(text_color="#00ff88" if (is_playing and snap.bpm > 0) else (pc if is_playing else DIM))
        self._onair_lbl.configure(text="◉" if on_air else " ")
        # Combine BLT + prodj — either source saying yes is enough
        pdj_master  = client is not None and "master" in client.state
        pdj_looping = (client is not None
                       and client.loop_start is not None
                       and client.loop_end   is not None
                       and client.play_state == "playing")
        is_master  = snap.is_master  or pdj_master
        is_looping = snap.is_looping or pdj_looping
        self._master_lbl.configure(text="MASTER" if is_master  else "", text_color="#f5d030")
        self._sync_lbl.configure(text="SYNC"   if snap.is_synced  else "", text_color=ACCENT2)
        self._loop_lbl.configure(text="LOOP"   if is_looping else "", text_color="#ff9900")

        title  = (snap.title  or "loading…")[:22]
        artist = (snap.artist or "")[:22]
        self._title_lbl.configure(text=title,  text_color=TEXT if is_playing else DIM)
        self._artist_lbl.configure(text=artist, text_color=DIM)

        bpm_actual = snap.bpm if snap.bpm > 0 else None
        pitch_pct  = ((snap.bpm / snap.track_bpm) - 1.0) * 100.0 if snap.track_bpm > 0 and snap.bpm > 0 else 0.0
        self._bpm_lbl.configure(text=f"{bpm_actual:.1f}" if bpm_actual else "—",
                                 text_color="#f5d030" if is_playing else DIM)
        if bpm_actual and abs(pitch_pct) >= 0.05:
            sign = "+" if pitch_pct >= 0 else ""
            self._pitch_lbl.configure(text=f"{sign}{pitch_pct:.1f}%", text_color=DIM)
        else:
            self._pitch_lbl.configure(text="", text_color=DIM)

        self._key_lbl.configure(text=snap.key or "—",
                                 text_color=ACCENT2 if snap.key else DIM)

        beat = snap.beat_within_bar or 0
        for i, d in enumerate(self._beat_dots, 1):
            if i == beat and is_playing:
                d.configure(text="●", text_color=TEXT)
            else:
                d.configure(text="○", text_color=DIM)

        phrase = _phrase_for_snap(snap)
        self._phrase_lbl.configure(text=phrase.upper() if phrase else "",
                                    text_color=_phrase_color(phrase))

        pos_s = snap.position_ms / 1000.0
        dur_s = snap.duration_ms / 1000.0 if snap.duration_ms > 0 else None
        if dur_s and dur_s > 0:
            self._pos_bar.set(min(1.0, pos_s / dur_s))
            self._pos_bar.configure(progress_color=pc)
        else:
            self._pos_bar.set(0)


class CDJTab(ctk.CTkFrame):
    def __init__(self, master, px_getter, restart_cb=None, ai_getter=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._px_getter    = px_getter
        self._restart_cb   = restart_cb
        self._ai_getter    = ai_getter  # callable → AILightControl instance (or None)
        self._retry_times  = {}     # player_number → last retry timestamp
        self._assigned          = [None, None, None]  # committed best/second/third
        self._player_loop_state = {}   # pn → bool: last known is_looping (for loop-release detect)
        self._transition_player      = None   # player_num of pending transition candidate
        self._transition_start_time  = 0.0    # wall clock when timer started
        self._transition_bpm         = 126.0  # BPM when timer started (sets 16-bar duration)
        self._last_trigger           = "HANDOFF"  # exposed to caller for tlog
        self._plan_tid_cache    = {}   # player_num → (track_id, bool(plan)) last rendered
        self._plan_active_phrase = {}  # player_num → last active_phrase_idx drawn
        self._plan_row_refs     = {}   # player_num → {pidx: row_widget}
        self._seen_decks         = set()  # player numbers ever observed — columns never removed
        self._build()

    def _build(self):
        # ── LED output strip ──────────────────────────────────────────────────
        strip_f = ctk.CTkFrame(self, height=22, fg_color="#050505", corner_radius=0)
        strip_f.pack(fill="x", pady=(0, 4))
        strip_f.pack_propagate(False)
        ctk.CTkLabel(strip_f, text="OUTPUT", font=("Helvetica Neue", 8),
                     text_color=DIM, width=46).pack(side="left", padx=4)
        self._led_strip = tk.Canvas(strip_f, bg="#050505", highlightthickness=0)
        self._led_strip.pack(side="left", fill="both", expand=True,
                              pady=3, padx=(0, 4))

        # ── Status row (hidden — info shown in main status bar instead) ─────
        status_row = ctk.CTkFrame(self, fg_color=BG2, height=28, corner_radius=4)
        # status_row.pack(fill="x", padx=4, pady=(0, 4))  # hidden
        status_row.pack_propagate(False)
        self._status_lbl = ctk.CTkLabel(status_row,
            text="ProDJ Link: connecting…" if PRODJ_OK else "ProDJ Link: not available",
            font=("Helvetica Neue", 9), text_color=DIM)
        self._status_lbl.pack(side="left", padx=10, pady=4)
        self._led_src_lbl = ctk.CTkLabel(status_row, text="",
            font=("Helvetica Neue", 9, "bold"), text_color=DIM)
        self._led_src_lbl.pack(side="right", padx=10, pady=4)

        # LED deck pin state (buttons live in the status bar, not here)
        self._led_pin = 0  # 0 = AUTO, 1-4 = pinned player
        self._pin_btns = {}
        if self._restart_cb:
            ctk.CTkButton(status_row, text="↺ BRIDGE", width=82, height=20,
                          font=("Helvetica Neue", 8, "bold"),
                          fg_color=BG3, hover_color="#2a3a2a",
                          border_color=BORDER, border_width=1,
                          text_color=DIM,
                          command=lambda: threading.Thread(target=self._restart_cb, daemon=True).start(),
                          ).pack(side="right", padx=6)

        # ── Player cards + timelines — fixed columns P1-P4, stacked per deck ──
        self._cards_f = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_f.pack(fill="x", padx=4, pady=(0, 4))
        for col in range(4):
            self._cards_f.columnconfigure(col, weight=1)
        self._cards = {}
        self._tl    = {}   # player_num → (wrapper_frame, tl_dict)
        self._plan_frames = {}   # player_num → CTkScrollableFrame
        for i in range(1, 5):
            card = PlayerCard(self._cards_f, i)
            self._cards[i] = card  # shown/hidden via grid/grid_remove in update()
            tl_wrap = ctk.CTkFrame(self._cards_f, fg_color=BG3, corner_radius=4)
            self._tl[i] = (tl_wrap, self._build_timeline_panel(tl_wrap))
            # Plan panel — row 2, fixed height scrollable list
            plan_outer = ctk.CTkFrame(self._cards_f, fg_color=BG2, corner_radius=4)
            plan_hdr = ctk.CTkFrame(plan_outer, fg_color="#0a0a12", corner_radius=0, height=18)
            plan_hdr.pack(fill="x")
            plan_hdr.pack_propagate(False)
            ctk.CTkLabel(plan_hdr, text=f"P{i}  LIGHT PLAN",
                         font=ctk.CTkFont(size=8, weight="bold"),
                         text_color="#3a6fc9", anchor="w").pack(side="left", padx=6)
            plan_scroll = ctk.CTkScrollableFrame(plan_outer, fg_color="transparent",
                                                  scrollbar_button_color=BG3,
                                                  scrollbar_button_hover_color=BG4,
                                                  height=220)
            plan_scroll.pack(fill="both", expand=True)
            self._plan_frames[i] = (plan_outer, plan_scroll)

    # ── Public update ─────────────────────────────────────────────────────────

    def update(self, prodj=None, blt=None):
        # LED strip
        px = self._px_getter()
        w  = self._led_strip.winfo_width()
        if w > 2 and px is not None and len(px) > 0:
            _draw_px_strip(self._led_strip, px, 22)

        # BLT status line
        if blt is not None:
            h       = blt.get_health()
            osc_age = h["osc"]["age"]
            snaps   = [blt.get_snapshot(n) for n in range(1, 5)]
            active  = [s for s in snaps if s.has_track or s.is_playing]
            n_act   = len(active)
            if osc_age < 3:
                st_col = "#00ff44"
                status = f"BLT: {n_act} player{'s' if n_act != 1 else ''}"
            elif osc_age < 10:
                st_col = "#ffaa00"
                status = f"BLT: last beat {osc_age:.0f}s ago"
            else:
                st_col = KILL_COL
                status = "BLT: no signal"
            self._status_lbl.configure(text=status, text_color=st_col)
        else:
            snaps  = []
            active = []
            self._status_lbl.configure(text="BLT: not connected", text_color=DIM)

        snap_by_pn = {s.player: s for s in snaps}

        # Waveform retry — prodj fetches color waveform for active BLT tracks
        now = time.perf_counter()
        if prodj is not None:
            for s in active:
                pn, tid = s.player, s.track_id
                if tid == 0:
                    continue
                if now - self._retry_times.get(pn, 0) > 5.0:
                    wvf_key = (pn, "usb", tid)
                    try:    prodj.data.color_waveform_store[wvf_key]; needs_wvf = False
                    except KeyError: needs_wvf = True
                    if needs_wvf:
                        self._retry_times[pn] = now
                        try:
                            prodj.data.get_color_waveform(pn, "usb", tid, lambda *a: None)
                            prodj.data.get_beatgrid(pn, "usb", tid, lambda *a: None)
                        except Exception as ex:
                            _log.debug("P%d waveform retry: %s", pn, ex)

        # tick_selection() already ran this frame from the app loop; resolve from committed assignment
        best   = snap_by_pn.get(self._assigned[0]) if self._assigned[0] else (active[0] if active else None)
        second = snap_by_pn.get(self._assigned[1]) if self._assigned[1] else (active[1] if len(active) > 1 else None)
        third  = snap_by_pn.get(self._assigned[2]) if self._assigned[2] else (active[2] if len(active) > 2 else None)

        # LED source indicator
        if best and best.is_playing:
            phrase = _phrase_for_snap(best)
            self._led_src_lbl.configure(
                text=f"LED ▶ P{best.player}  {phrase.upper() if phrase else ''}",
                text_color=KILL_COL if best.on_air else ACCENT)
        else:
            self._led_src_lbl.configure(text="LED ▶ —", text_color=DIM)

        # Build prodj client lookup by player number
        pdj_by_pn = {}
        if prodj is not None:
            try:
                for c in prodj.cl.clients:
                    pdj_by_pn[c.player_number] = c
            except Exception:
                pass

        # Track which decks have ever been seen — their columns stay visible permanently
        for pn, s in snap_by_pn.items():
            if s.has_track or s.is_playing:
                self._seen_decks.add(pn)

        # Show all seen players; always in fixed column order P1-P4
        # Card at row=0, timeline at row=1, plan panel at row=2 — same column
        ai_ctrl = self._ai_getter() if self._ai_getter else None
        for i in range(1, 5):
            snap_i = snap_by_pn.get(i)
            tl_wrap, tl = self._tl[i]
            plan_outer, plan_scroll = self._plan_frames[i]
            if i in self._seen_decks:
                self._cards[i].grid(row=0, column=i - 1, sticky="nsew", padx=2, pady=(0, 2))
                self._cards[i].update(snap_i, client=pdj_by_pn.get(i))
                tl_wrap.grid(row=1, column=i - 1, sticky="nsew", padx=2, pady=(0, 2))
                self._update_timeline(tl, snap_i, prodj)
                plan_outer.grid(row=2, column=i - 1, sticky="nsew", padx=2, pady=(0, 4))
                try:
                    self._update_plan_panel(i, snap_i, plan_scroll, ai_ctrl)
                except Exception as _pp_exc:
                    _log.debug("plan panel P%d: %s", i, _pp_exc)
            else:
                self._cards[i].grid_remove()
                tl_wrap.grid_remove()
                plan_outer.grid_remove()
                self._plan_tid_cache.pop(i, None)
                self._plan_active_phrase.pop(i, None)

    def refresh_plan(self, track_id=None):
        """Push-triggered rebuild: clear plan panel cache for the given track (or all)."""
        if track_id is None:
            self._plan_tid_cache.clear()
            self._plan_active_phrase.clear()
        else:
            for pn, key in list(self._plan_tid_cache.items()):
                if key is not None and key[0] == track_id:
                    self._plan_tid_cache.pop(pn, None)
                    self._plan_active_phrase.pop(pn, None)

    # ── Player selection (hysteresis) ──────────────────────────────────────

    def _set_led_pin(self, val):
        self._led_pin = val
        for v, b in self._pin_btns.items():
            active = (v == val)
            b.configure(fg_color=ACCENT if active else BG3,
                        text_color="#000000" if active else DIM)

    def tick_selection(self, blt, prodj=None):
        """Returns committed LED deck.
        Master = deck whose phrases are moving forward (playing AND not looped).
        """
        if self._led_pin:
            return self._led_pin
        if blt is None:
            return self._assigned[0] or 0

        now    = time.time()
        snaps  = [blt.get_snapshot(n) for n in range(1, 5)]

        # Fast loop state from prodj (doesn't wait for 1s HTTP poll).
        # prodj detects loop_start/loop_end in real time — used alongside
        # snap.is_looping so loops are detected as soon as the DJ sets them.
        _fast_loops: dict = {}
        if prodj is not None:
            try:
                for _cl in prodj.cl.clients:
                    _pn = getattr(_cl, "player_number", None)
                    if _pn is None:
                        continue
                    _fast_loops[_pn] = (
                        _cl.loop_start is not None
                        and _cl.loop_end is not None
                        and getattr(_cl, "play_state", None) == "playing"
                    )
            except Exception:
                pass

        def _prog(s):
            """Deck is progressing through phrases: playing, not looped, has BPM."""
            return bool(
                s and s.is_playing
                and not s.is_looping
                and not _fast_loops.get(s.player, False)
                and (s.bpm or 0) > 0
            )

        committed      = self._assigned[0]
        committed_snap = blt.get_snapshot(committed) if committed else None
        all_prog       = [s for s in snaps if _prog(s)]

        # ── Update per-deck loop state (needed for loop-release detection) ──
        active_pns = {s.player for s in snaps if s.is_playing}
        for s in snaps:
            if s.is_playing:
                self._player_loop_state[s.player] = s.is_looping
        for pn in list(self._player_loop_state):
            if pn not in active_pns:
                self._player_loop_state[pn] = False

        # ── Rule 1: Master loops → stay on it, only switch on loop release ──
        # Do NOT switch away just because master enters a loop.
        # Phrase display freezes naturally (looping deck excluded from _prog).
        # Loop-release switch is handled in Rule 4 (just_released path).

        # ── Rule 2: No committed yet, or committed deck fully stopped ──
        if not committed or (committed_snap and not committed_snap.is_playing):
            if all_prog:
                best = max(all_prog, key=lambda s: s.bpm or 0)
                self._assigned[0]       = best.player
                self._transition_player = None
                self._last_trigger      = "HANDOFF"
            # Update display slots and return
            others = [s for s in snaps if s.is_playing and s.player != self._assigned[0]]
            self._assigned[1] = others[0].player if others else None
            self._assigned[2] = others[1].player if len(others) > 1 else None
            return self._assigned[0] or 0

        # ── Rule 3: Only one progressing deck ──
        if len(all_prog) == 1:
            solo = all_prog[0]
            if solo.player != self._assigned[0]:
                _log.info("P%d only progressing deck → switch from P%d",
                          solo.player, self._assigned[0])
                self._assigned[0]       = solo.player
                self._transition_player = None
                self._last_trigger      = "SOLE_PROGRESSING"
            else:
                self._transition_player = None  # no competitor
            others = [s for s in snaps if s.is_playing and s.player != self._assigned[0]]
            self._assigned[1] = others[0].player if others else None
            self._assigned[2] = others[1].player if len(others) > 1 else None
            return self._assigned[0]

        # ── Rule 4: Multiple progressing decks — manage 16-bar transition timer ──
        if _prog(blt.get_snapshot(self._assigned[0])):
            # Current master is progressing. Check for competitors.
            others_prog = [s for s in all_prog if s.player != self._assigned[0]]
            if not others_prog:
                self._transition_player = None
            else:
                best_other = max(others_prog, key=lambda s: s.bpm or 0)

                if self._transition_player != best_other.player:
                    # New competitor — (re)start 16-bar timer ONLY if not looping
                    if best_other.is_looping:
                        self._transition_player = None  # loaded + looping = not ready
                    else:
                        self._transition_player     = best_other.player
                        self._transition_start_time = now
                        self._transition_bpm        = best_other.bpm or 126.0
                        _log.info("P%d progressing — 16-bar transition timer started (master=P%d)",
                                  best_other.player, self._assigned[0])
                else:
                    # Existing timer: check for loop-release early-switch
                    trans_snap   = blt.get_snapshot(self._transition_player)
                    was_looping  = self._player_loop_state.get(self._transition_player, False)
                    just_released = was_looping and trans_snap and not trans_snap.is_looping
                    if just_released:
                        bts = (trans_snap.beats_to_next_phrase or 0)
                        if 0 < bts <= 64:   # within 16 bars of next phrase boundary
                            _log.info("P%d loop released within 16 bars of phrase → switch",
                                      self._transition_player)
                            self._assigned[0]       = self._transition_player
                            self._transition_player = None
                            self._last_trigger      = "LOOP_RELEASE_PHRASE"

                    # If transition candidate is looping → cancel timer BEFORE expiry check
                    # (prevents brief switch to a looping deck when timer expires simultaneously)
                    if self._transition_player and trans_snap and trans_snap.is_looping:
                        _log.info("P%d transition candidate looped → timer cancelled",
                                  self._transition_player)
                        self._transition_player = None

                    # Timer expiry — only fire if transition deck is on-air (mixed in)
                    if self._transition_player:
                        beat_dur  = 60.0 / max(self._transition_bpm, 60.0)
                        timer_dur = 16 * 4 * beat_dur  # 16 bars
                        trans_snap = blt.get_snapshot(self._transition_player)
                        if (now - self._transition_start_time >= timer_dur
                                and trans_snap and trans_snap.on_air):
                            _log.info("P%d 16-bar timer complete → switch from P%d",
                                      self._transition_player, self._assigned[0])
                            self._assigned[0]       = self._transition_player
                            self._transition_player = None
                            self._last_trigger      = "TRANSITION_TIMER"

        elif all_prog:
            # ── Rule 5: Committed not progressing (stopped/looped handled above) ──
            best = max(all_prog, key=lambda s: s.bpm or 0)
            _log.info("P%d not progressing → switch to P%d", self._assigned[0], best.player)
            self._assigned[0]       = best.player
            self._transition_player = None
            self._last_trigger      = "HANDOFF"

        # Update display slots 1/2
        others = [s for s in snaps if s.is_playing and s.player != self._assigned[0]]
        self._assigned[1] = others[0].player if others else None
        self._assigned[2] = others[1].player if len(others) > 1 else None
        return self._assigned[0] or 0

    def _pick_players(self, snaps):
        """Return (best, second) snapshots with hysteresis on player number."""
        HYSTERESIS = 64  # ~2s at 30fps — prevents rapid flip-flopping during mixes

        def _score(s):
            return (s.is_master, s.on_air, s.is_playing, s.bpm > 0, s.position_ms)

        snap_by_pn = {s.player: s for s in snaps}
        ranked_pns = [s.player for s in sorted(snaps, key=_score, reverse=True)]

        result = []
        for slot in range(3):
            new_pn = ranked_pns[slot] if len(ranked_pns) > slot else None
            if new_pn == self._cand[slot]:
                self._cand_n[slot] += 1
            else:
                self._cand[slot]   = new_pn
                self._cand_n[slot] = 1
            if self._cand_n[slot] >= HYSTERESIS:
                self._assigned[slot] = new_pn
            committed_pn = self._assigned[slot] if self._assigned[slot] is not None else new_pn
            result.append(snap_by_pn.get(committed_pn))

        return result[0], result[1], result[2]

    # ── Plan panel ────────────────────────────────────────────────────────

    _PLAN_KIND_COLORS = {
        "intro":   "#1a6b3a",
        "up":      "#7c2a9e",
        "chorus":  "#b8181e",
        "down":    "#1a4a8c",
        "outro":   "#4a3000",
        "verse":   "#2d4a1e",
        "bridge":  "#3d3d00",
        "drop":    "#8c1a1a",
    }

    def _update_plan_panel(self, player_num, snap, scroll_frame, ai_ctrl):
        """Rebuild plan rows inside scroll_frame only when track_id changes."""
        if snap is None or not snap.has_track or ai_ctrl is None:
            if self._plan_tid_cache.get(player_num) is not None:
                for w in scroll_frame.winfo_children():
                    w.destroy()
                self._plan_tid_cache[player_num] = None
                no_lbl = ctk.CTkLabel(scroll_frame, text="No plan",
                                      font=ctk.CTkFont(size=9), text_color=DIM)
                no_lbl.pack(pady=8)
            return

        track_id = snap.track_id
        plan = ai_ctrl._track_plans.get(track_id)

        # Determine active phrase index from snap
        phrases = snap.phrases if snap.phrases else ()
        pos_ms  = snap.position_ms
        active_phrase_idx = 0
        if phrases:
            # Prefer current_phrase (OSC push, instant) over pos_ms dead-reckoning
            if snap.current_phrase:
                cur_bs = snap.current_phrase.beat_start
                for idx, ph in enumerate(phrases):
                    if ph.beat_start == cur_bs:
                        active_phrase_idx = idx
                        break
                else:
                    # beat_start not found exactly — nearest
                    active_phrase_idx = min(range(len(phrases)),
                                           key=lambda i: abs(phrases[i].beat_start - cur_bs))
            else:
                for idx, ph in enumerate(phrases):
                    beat_s = (ph.beat_start - 1) * 60.0 / (snap.bpm or 120.0)
                    if beat_s * 1000.0 <= pos_ms:
                        active_phrase_idx = idx

        # Cache key includes whether plan data is available — so "Analyzing…" → real plan
        # forces a rebuild even though track_id hasn't changed.
        _cache_key = (track_id, bool(plan))
        if self._plan_tid_cache.get(player_num) != _cache_key:
            # NOTE: cache key written AFTER successful render (below) to avoid
            # marking incomplete builds as done if an exception occurs mid-render.
            for w in scroll_frame.winfo_children():
                w.destroy()
            self._plan_row_refs[player_num] = {}

            if not plan:
                wait_lbl = ctk.CTkLabel(scroll_frame, text="Analyzing…",
                                        font=ctk.CTkFont(size=9, weight="bold"),
                                        text_color="#555577")
                wait_lbl.pack(pady=14)
                self._plan_tid_cache[player_num] = _cache_key
                return

            # ── Build cue-point lookup: beat → list of cues ───────────────
            cues     = list(snap.cues) if snap.cues else []
            _cbpm    = snap.bpm or 120.0

            def _cue_beat(c):
                if c.beat and c.beat > 0:
                    return c.beat
                if c.time_ms and c.time_ms > 0:
                    return int(c.time_ms / 1000.0 * _cbpm / 60.0) + 1
                return 0

            def _cue_color(c):
                col = getattr(c, "color", None) or "#ffcc00"
                if isinstance(col, (list, tuple)) and len(col) >= 3:
                    return "#{:02x}{:02x}{:02x}".format(
                        int(col[0]), int(col[1]), int(col[2]))
                return col

            # Map each cue to the phrase it belongs to (by beat range)
            sorted_keys  = sorted(plan.keys())
            cues_in_phrase = {k: [] for k in sorted_keys}  # pidx → [cues]
            for cue in sorted(cues, key=_cue_beat):
                cb = _cue_beat(cue)
                placed = False
                for pidx in sorted_keys:
                    e = plan[pidx]
                    bs = e.get("beat_start") or 0
                    be = e.get("beat_end") or 999999
                    if bs <= cb < be:
                        cues_in_phrase[pidx].append(cue)
                        placed = True
                        break
                if not placed and sorted_keys:
                    cues_in_phrase[sorted_keys[-1]].append(cue)

            # ── Combined phrase + cue timeline ────────────────────────────
            for pidx in sorted_keys:
                entry = plan[pidx]
                kind        = entry.get("kind", "")
                effect      = entry.get("effect", "")
                palette     = entry.get("palette", "")
                brightness  = entry.get("brightness", 1.0)
                speed       = entry.get("speed", 1.0)
                beat_start  = entry.get("beat_start", 0)
                beat_end    = entry.get("beat_end", 0)
                kind_col    = self._PLAN_KIND_COLORS.get(kind, "#2a2a3a")
                is_active   = (pidx == active_phrase_idx and snap.is_playing)
                ph_cues     = cues_in_phrase.get(pidx, [])
                # Cue at the very start of this phrase (same beat) → badge on row
                start_cue   = next((c for c in ph_cues if abs(_cue_beat(c) - beat_start) <= 1), None)
                inner_cues  = [c for c in ph_cues if c is not start_cue]

                row_bg = "#181828" if is_active else "#0e0e18"
                row = ctk.CTkFrame(scroll_frame, fg_color=row_bg, corner_radius=3,
                                   border_color="#3a6fc9" if is_active else "#0e0e18",
                                   border_width=1 if is_active else 0)
                row.pack(fill="x", padx=4, pady=(2, 0))
                self._plan_row_refs.setdefault(player_num, {})[pidx] = row

                # Kind badge
                badge = ctk.CTkFrame(row, fg_color=kind_col, corner_radius=2, width=44, height=14)
                badge.pack(side="left", padx=(5, 3), pady=4)
                badge.pack_propagate(False)
                ctk.CTkLabel(badge, text=kind.upper()[:5],
                             font=ctk.CTkFont(size=7, weight="bold"),
                             text_color=TEXT).pack(expand=True)

                # Cue badge at phrase start
                if start_cue:
                    sc_col = _cue_color(start_cue)
                    sc_lbl = start_cue.label or ""
                    cue_dot = tk.Canvas(row, width=8, height=8, bg=row_bg, highlightthickness=0)
                    cue_dot.create_oval(1, 1, 7, 7, fill=sc_col, outline="")
                    cue_dot.pack(side="left", padx=(0, 1), pady=6)
                    if sc_lbl:
                        ctk.CTkLabel(row, text=sc_lbl,
                                     font=ctk.CTkFont(size=8, weight="bold"),
                                     text_color=sc_col, width=10).pack(side="left")

                # Effect name
                eff_short = effect[:11] if len(effect) > 11 else effect
                ctk.CTkLabel(row, text=eff_short,
                             font=ctk.CTkFont(size=9, weight="bold"),
                             text_color="#c8c8e8").pack(side="left", padx=(2, 1))

                if palette:
                    ctk.CTkLabel(row, text=f"/{palette}",
                                 font=ctk.CTkFont(size=8),
                                 text_color="#7a7aaa").pack(side="left")

                # Metrics: brightness + speed bars (right side)
                bri_pct = max(0.0, min(1.0, float(brightness)))
                spd_pct = max(0.0, min(1.0, float(speed) / 3.0))
                metrics = ctk.CTkFrame(row, fg_color=row_bg)
                metrics.pack(side="right", padx=5)
                bri_cv = tk.Canvas(metrics, width=26, height=4, bg=row_bg, highlightthickness=0)
                bri_cv.pack(side="top", pady=(2, 0))
                bri_cv.create_rectangle(0, 0, int(26 * bri_pct), 4, fill="#6a6aaa", outline="")
                bri_cv.create_rectangle(int(26 * bri_pct), 0, 26, 4, fill="#1a1a2a", outline="")
                spd_cv = tk.Canvas(metrics, width=26, height=4, bg=row_bg, highlightthickness=0)
                spd_cv.pack(side="top", pady=(1, 2))
                spd_col = "#3a9a3a" if spd_pct > 0.6 else "#9a7a1a" if spd_pct > 0.3 else "#1a5a8a"
                spd_cv.create_rectangle(0, 0, int(26 * spd_pct), 4, fill=spd_col, outline="")
                spd_cv.create_rectangle(int(26 * spd_pct), 0, 26, 4, fill="#1a1a2a", outline="")

                mid_changes = entry.get("mid_phrase_changes", [])
                if mid_changes:
                    ctk.CTkLabel(row, text=f"+{len(mid_changes)}m",
                                 font=ctk.CTkFont(size=7),
                                 text_color="#4a6a4a").pack(side="right", padx=2)

                # Inner cue points — small sub-rows indented under this phrase
                for cue in inner_cues:
                    c_ms  = cue.time_ms
                    c_mm, c_ss = divmod(int(c_ms / 1000), 60)
                    c_col = _cue_color(cue)
                    c_lbl = cue.label or ""
                    c_nm  = cue.name or ""
                    ctype = getattr(cue, "type", "hot_cue")
                    crow = ctk.CTkFrame(scroll_frame, fg_color="#0a0a12", corner_radius=2)
                    crow.pack(fill="x", padx=(16, 4), pady=1)
                    # vertical connector line
                    conn = tk.Canvas(crow, width=6, height=14, bg="#0a0a12", highlightthickness=0)
                    conn.create_line(3, 0, 3, 7, fill="#333344")
                    conn.create_oval(1, 7, 5, 11, fill=c_col, outline="")
                    conn.pack(side="left", padx=(4, 2))
                    if c_lbl:
                        ctk.CTkLabel(crow, text=c_lbl,
                                     font=ctk.CTkFont(size=8, weight="bold"),
                                     text_color=c_col, width=10).pack(side="left")
                    ctk.CTkLabel(crow, text=f"{c_mm}:{c_ss:02d}",
                                 font=ctk.CTkFont(size=8),
                                 text_color="#8888aa", width=32).pack(side="left")
                    if c_nm:
                        ctk.CTkLabel(crow, text=c_nm,
                                     font=ctk.CTkFont(size=8),
                                     text_color=DIM).pack(side="left", padx=2)
                    if ctype in ("loop", "memory"):
                        ctk.CTkLabel(crow, text=ctype[:3].upper(),
                                     font=ctk.CTkFont(size=7),
                                     text_color="#555577").pack(side="right", padx=3)

            # Mark render complete only after successful build
            self._plan_tid_cache[player_num] = _cache_key
            self._plan_active_phrase[player_num] = active_phrase_idx
            # Scroll to active row after geometry settles
            _active_row = self._plan_row_refs.get(player_num, {}).get(active_phrase_idx)
            if _active_row is not None:
                scroll_frame.after(40, lambda sf=scroll_frame, r=_active_row:
                                   self._scroll_plan_to_row(sf, r))
            return

        # Track+plan haven't changed — rebuild only when active phrase changes
        if self._plan_active_phrase.get(player_num) != active_phrase_idx:
            self._plan_active_phrase[player_num] = active_phrase_idx
            # Invalidate cache so next call rebuilds with new highlight
            self._plan_tid_cache.pop(player_num, None)
            self._update_plan_panel(player_num, snap, scroll_frame, ai_ctrl)

    @staticmethod
    def _scroll_plan_to_row(scroll_frame, row):
        """Scroll CTkScrollableFrame so the active row is visible near the top."""
        try:
            canvas     = scroll_frame._parent_canvas
            content_h  = scroll_frame.winfo_reqheight()
            canvas_h   = canvas.winfo_height()
            if content_h <= canvas_h or content_h <= 0:
                return  # content fits — no scroll needed
            row_y  = row.winfo_y()
            row_h  = row.winfo_height()
            # Target: row centred in the visible area, biased toward showing rows ahead
            target_top = row_y - max(10, (canvas_h - row_h) // 3)
            fraction   = max(0.0, min(1.0, target_top / content_h))
            canvas.yview_moveto(fraction)
        except Exception:
            pass

    # ── Timeline panel builders ────────────────────────────────────────────

    def _build_timeline_panel(self, parent):
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=6, pady=(4, 2))
        p_lbl = ctk.CTkLabel(hdr, text="—",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=DIM)
        p_lbl.pack(side="left")
        ph_lbl = ctk.CTkLabel(hdr, text="",
            font=ctk.CTkFont(size=9, weight="bold"), text_color=DIM)
        ph_lbl.pack(side="left", padx=6)
        next_lbl = ctk.CTkLabel(hdr, text="",
            font=ctk.CTkFont(size=9), text_color=DIM)
        next_lbl.pack(side="left")
        t_lbl = ctk.CTkLabel(hdr, text="—:— / —:—",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=DIM)
        t_lbl.pack(side="right")
        wvf_cv = tk.Canvas(parent, height=48, bg="#06060e", highlightthickness=0)
        wvf_cv.pack(fill="x", padx=6, pady=(0, 1))
        phr_cv = tk.Canvas(parent, height=12, bg=BG3, highlightthickness=0)
        phr_cv.pack(fill="x", padx=6, pady=(0, 1))
        time_cv = tk.Canvas(parent, height=16, bg=BG3, highlightthickness=0)
        time_cv.pack(fill="x", padx=6, pady=(0, 6))
        return {"player_lbl": p_lbl, "phrase_lbl": ph_lbl, "next_lbl": next_lbl,
                "time_lbl": t_lbl,
                "waveform_cv": wvf_cv, "phrase_cv": phr_cv, "time_cv": time_cv}

    def _update_timeline(self, tl, snap, prodj):
        if snap is None or not snap.has_track:
            tl["player_lbl"].configure(text="—", text_color=DIM)
            tl["phrase_lbl"].configure(text="", text_color=DIM)
            tl["time_lbl"].configure(text="—:— / —:—", text_color=DIM)
            for cv_key in ("waveform_cv", "phrase_cv", "time_cv"):
                tl[cv_key].delete("all")
            return

        pn     = snap.player
        pos_s  = snap.position_ms / 1000.0
        dur_s  = snap.duration_ms / 1000.0 if snap.duration_ms > 0 else None

        is_playing = snap.is_playing
        on_air     = snap.on_air
        p_col = KILL_COL if on_air else (ACCENT if is_playing else DIM)
        tl["player_lbl"].configure(text=f"P{pn}", text_color=p_col)

        phrase_display = _phrase_for_snap(snap)
        phrase_kind    = snap.current_phrase.kind if snap.current_phrase else ""
        tl["phrase_lbl"].configure(text=phrase_display.upper() if phrase_display else "",
                                    text_color=_phrase_color(phrase_kind))

        # Time to next phrase
        bpm = snap.bpm if snap.bpm > 0 else 120.0
        btns = snap.beats_to_next_phrase
        if btns > 0 and bpm > 0:
            secs = btns * 60.0 / bpm
            sm, ss = divmod(int(secs), 60)
            tl["next_lbl"].configure(text=f"→ {sm}:{ss:02d}", text_color=DIM)
        else:
            tl["next_lbl"].configure(text="")

        if dur_s:
            pm, ps = divmod(int(max(0, pos_s)), 60)
            dm, ds = divmod(int(dur_s), 60)
            tl["time_lbl"].configure(text=f"{pm}:{ps:02d} / {dm}:{ds:02d}", text_color=TEXT)
        else:
            pm, ps = divmod(int(max(0, pos_s)), 60)
            tl["time_lbl"].configure(text=f"{pm}:{ps:02d}", text_color=DIM)

        # Waveform from prodj NFS store
        waveform = None
        if prodj is not None:
            wvf_key = (pn, "usb", snap.track_id)
            try:    waveform = prodj.data.color_waveform_store[wvf_key]
            except KeyError: pass
            if waveform is None:
                try:    waveform = prodj.data.waveform_store[wvf_key]
                except KeyError: pass

        # Cues and phrases from BLT snapshot
        cues    = list(snap.cues)    if snap.cues    else []
        phrases = snap.phrases       if snap.phrases  else ()

        track_bpm  = snap.bpm if snap.bpm > 0 else 0.0
        beat_anchor = snap.beat_number   # absolute beat at pos_s — anchor for beat→time
        self._draw_waveform(waveform, dur_s, pos_s, tl["waveform_cv"],
                            phrases=phrases, cues=cues,
                            track_bpm=track_bpm, beat_anchor=beat_anchor)
        self._draw_timeline(pos_s, phrases, dur_s, tl["phrase_cv"],
                            track_bpm=track_bpm, beat_anchor=beat_anchor,
                            cues=cues)
        self._draw_time_ruler(dur_s, pos_s, tl["time_cv"])

    # ── Draw helpers ──────────────────────────────────────────────────────

    def _beat_to_s(self, beat, track_bpm, pos_s, beat_anchor):
        """
        Convert absolute beat number to seconds, anchored to the live playhead.
        Uses current position_ms + beat_number as ground truth, eliminating
        accumulated BPM drift regardless of pitch changes.
        """
        if track_bpm > 0 and beat_anchor > 0 and pos_s is not None:
            return pos_s + (beat - beat_anchor) * 60.0 / track_bpm
        elif track_bpm > 0:
            return (beat - 1) * 60.0 / track_bpm
        return 0.0

    def _draw_waveform(self, waveform, dur_s, pos_s, cv, phrases=None, cues=None,
                       track_bpm=0, beat_anchor=0):
        """PIL/numpy scrolling 30-second viewport — single image blit per frame."""
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w < 4 or h < 4:
            return

        VIEW_S = dur_s if dur_s else 30.0   # full track visible

        if pos_s is None:
            pos_s = 0.0

        view_start = 0.0
        head_x     = int(pos_s / VIEW_S * w) if VIEW_S > 0 else 0

        # Build pixel array
        arr = np.full((h, w, 3), [6, 6, 14], dtype=np.uint8)
        mid   = h // 2
        scale = mid / 32.0

        if waveform and len(waveform) > 0:
            n_cols   = len(waveform)
            is_color = not isinstance(waveform, (bytes, bytearray))

            for px_x in range(w):
                t = view_start + (px_x / w) * VIEW_S
                if t < 0.0:
                    continue
                if dur_s and t > dur_s:
                    continue

                # Map time → waveform column
                if dur_s and dur_s > 0.0:
                    wvf_idx = int(t / dur_s * n_cols)
                else:
                    wvf_idx = int(px_x / w * n_cols)
                wvf_idx = max(0, min(n_cols - 1, wvf_idx))

                v = waveform[wvf_idx]
                if is_color:
                    # PWV5: bits 2-6=height, 13-15=red, 10-12=green, 7-9=blue
                    ht = (v >> 2)  & 0x1F
                    r  = ((v >> 13) & 0x07) * 36
                    g  = ((v >> 10) & 0x07) * 36
                    b  = ((v >>  7) & 0x07) * 36
                else:
                    # Plain PWAV: bits 3-7=height, 0-2=whiteness
                    ht    = (v >> 3) & 0x1F
                    white = (v & 0x07) * 28
                    r     = white // 3
                    g     = white // 2
                    b     = 80 + white

                px_h = max(2, int(ht * scale))
                y0   = max(0, mid - px_h)
                y1   = min(h, mid + px_h)
                arr[y0:y1, px_x] = [r, g, b]

        else:
            # No waveform — dim placeholder
            arr[mid - 2:mid + 2, ::2] = [26, 26, 42]

        # Phrase band — bottom 5 rows (BLT Phrase dataclass)
        if phrases and dur_s and dur_s > 0:
            entries = list(phrases)
            for idx, p in enumerate(entries):
                t_start = self._beat_to_s(p.beat_start, track_bpm, pos_s, beat_anchor)
                t_end   = self._beat_to_s(entries[idx + 1].beat_start, track_bpm, pos_s, beat_anchor) \
                          if idx + 1 < len(entries) else dur_s
                x1 = int((t_start - view_start) / VIEW_S * w)
                x2 = int((t_end   - view_start) / VIEW_S * w)
                x1, x2 = max(0, x1), min(w, max(x1 + 1, x2))
                if x2 > x1:
                    ch  = _phrase_color(p.kind)
                    rgb = (int(ch[1:3], 16), int(ch[3:5], 16), int(ch[5:7], 16))
                    arr[h - 5:h, x1:x2] = rgb

        # Hot cue vertical markers (BLT CuePoint dataclass)
        if cues and dur_s and dur_s > 0:
            for cue in cues:
                tm = cue.time_ms
                if (not tm) and cue.beat > 0 and track_bpm > 0:
                    tm = int(self._beat_to_s(cue.beat, track_bpm, pos_s, beat_anchor) * 1000)
                t_cue = tm / 1000.0
                cx    = int((t_cue - view_start) / VIEW_S * w)
                if 0 <= cx < w:
                    cr = cue.color[0] if cue.color else 200
                    cg = cue.color[1] if cue.color else 200
                    cb = cue.color[2] if cue.color else 200
                    x0 = max(0, cx - 1)
                    x1 = min(w, cx + 1)
                    arr[:h - 5, x0:x1] = [cr, cg, cb]

        # Loop regions from cue list (type == "loop")
        if cues and dur_s and dur_s > 0:
            for cue in cues:
                if cue.type != "loop" or not cue.loop_end_ms:
                    continue
                t0 = (cue.time_ms or 0) / 1000.0
                t1 = cue.loop_end_ms / 1000.0
                if t0 == 0 and cue.beat > 0 and track_bpm > 0:
                    t0 = self._beat_to_s(cue.beat, track_bpm, pos_s, beat_anchor)
                lx0 = max(0, int((t0 - view_start) / VIEW_S * w))
                lx1 = min(w, int((t1 - view_start) / VIEW_S * w))
                if lx1 > lx0:
                    # Tint loop region orange
                    arr[:h - 5, lx0:lx1] = np.clip(
                        arr[:h - 5, lx0:lx1].astype(np.int16) + [30, 15, 0], 0, 255
                    ).astype(np.uint8)
                    arr[0:3, lx0:lx1] = [255, 140, 0]   # orange top bar

        # Playhead
        hx0 = max(0, head_x - 1)
        hx1 = min(w, head_x + 1)
        arr[:, hx0:hx1] = [255, 255, 255]

        photo = ImageTk.PhotoImage(Image.fromarray(arr))
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=photo)
        cv._wf_photo = photo   # prevent GC

    def _draw_timeline(self, pos_s, phrases, dur_s, cv, track_bpm=0, beat_anchor=0, cues=None):
        """Phrase overview bar — full track bird's-eye view (BLT Phrase dataclass)."""
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w < 4 or h < 2:
            return

        entries = list(phrases) if phrases else []
        dur_s   = dur_s or 0.0

        arr = np.full((h, w, 3), [26, 26, 42], dtype=np.uint8)

        if entries and dur_s > 0:
            for idx, p in enumerate(entries):
                t_start = self._beat_to_s(p.beat_start, track_bpm, pos_s, beat_anchor)
                t_end   = self._beat_to_s(entries[idx + 1].beat_start, track_bpm, pos_s, beat_anchor) \
                          if idx + 1 < len(entries) else dur_s
                x1 = int(t_start / dur_s * w)
                x2 = int(t_end   / dur_s * w)
                x1, x2 = max(0, x1), min(w, max(x1 + 1, x2))
                if x2 > x1:
                    ch  = _phrase_color(p.kind)
                    rgb = (int(ch[1:3], 16), int(ch[3:5], 16), int(ch[5:7], 16))
                    arr[1:h - 1, x1:x2] = rgb

        # Cue triangles (downward-pointing, top 6 rows)
        if cues and dur_s > 0:
            for cue in cues:
                tm = cue.time_ms
                if (not tm) and cue.beat > 0 and track_bpm > 0:
                    tm = int(self._beat_to_s(cue.beat, track_bpm, pos_s, beat_anchor) * 1000)
                if not tm:
                    continue
                cx = int((tm / 1000.0) / dur_s * w)
                if not (0 <= cx < w):
                    continue
                cr = cue.color[0] if cue.color else 255
                cg = cue.color[1] if cue.color else 255
                cb = cue.color[2] if cue.color else 255
                # Downward triangle: wide at top (row 0), point at row 5
                for dy in range(6):
                    half = 3 - dy // 2
                    x0 = max(0, cx - half)
                    x1 = min(w, cx + half + 1)
                    arr[dy, x0:x1] = [cr, cg, cb]

        # Playhead
        if pos_s is not None and dur_s > 0:
            ph_x = max(0, min(w - 1, int(pos_s / dur_s * w)))
            arr[:, max(0, ph_x - 1):min(w, ph_x + 1)] = [255, 255, 255]

        photo = ImageTk.PhotoImage(Image.fromarray(arr))
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=photo)
        cv._tl_photo = photo

    def _draw_time_ruler(self, dur_s, pos_s, cv):
        """Time ruler showing the current 30-second viewport window."""
        w = cv.winfo_width()
        if w < 4:
            return

        if pos_s is None:
            pos_s = 0.0
        VIEW_S = dur_s if dur_s else 30.0
        head_x = int(pos_s / VIEW_S * w) if VIEW_S > 0 else 0

        cv.delete("all")
        cv.create_rectangle(0, 0, w, 18, fill=BG2, outline="")

        # Current position label (left of playhead)
        m, s = divmod(int(max(0, pos_s)), 60)
        cv.create_text(2, 9, text=f"{m}:{s:02d}", fill=TEXT,
                       font=("Helvetica Neue", 8), anchor="w")

        # Remaining time (right edge)
        if dur_s and dur_s > 0:
            rem = max(0, int(dur_s - pos_s))
            rm, rs = divmod(rem, 60)
            cv.create_text(w - 2, 9, text=f"-{rm}:{rs:02d}", fill=DIM,
                           font=("Helvetica Neue", 8), anchor="e")

            # Tick marks at regular intervals across full track
            interval = 30.0 if dur_s <= 300 else 60.0
            t = interval
            while t < dur_s:
                x      = int(t / dur_s * w)
                tm, ts = divmod(int(t), 60)
                lbl    = f"{tm}:{ts:02d}" if ts == 0 else f":{ts:02d}"
                cv.create_line(x, 0, x, 6, fill="#333333")
                cv.create_text(x, 13, text=lbl, fill="#444444",
                               font=("Helvetica Neue", 7), anchor="center")
                t += interval

        # Playhead tick
        cv.create_line(head_x, 0, head_x, 18, fill="white", width=1)



# ══════════════════════════════════════════════════════════════════════════════
# LED Control tab — geometry canvas + effects
# ══════════════════════════════════════════════════════════════════════════════

class GeometryEditorDialog(ctk.CTkToplevel):
    def __init__(self, master, device, on_save):
        super().__init__(master)
        self.title(f"Configure Layout — {device.name}")
        self.geometry("540x520")
        self.resizable(False, False)
        self.configure(fg_color=BG2)
        self.after(50, lambda: (self.lift(), self.focus_force(), self.grab_set()))
        self._device  = device
        self._on_save = on_save
        self._type_var = ctk.StringVar(value=device.geometry.get("type", "linear"))
        self._build()
        self._switch_type(self._type_var.get())

    def _build(self):
        ctk.CTkLabel(self, text="GEOMETRY EDITOR",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=ACCENT).pack(pady=(12, 4))
        tr = ctk.CTkFrame(self, fg_color="transparent")
        tr.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(tr, text="Type:", font=MONO9, text_color=DIM).pack(side="left", padx=(0, 8))
        for t in ("linear", "snake", "globe"):
            ctk.CTkRadioButton(tr, text=t.upper(), variable=self._type_var, value=t,
                               font=MONO9, text_color=TEXT,
                               fg_color=ACCENT, hover_color=ACCENT,
                               command=lambda t=t: self._switch_type(t)
                               ).pack(side="left", padx=8)
        ctk.CTkFrame(self, height=1, fg_color=BORDER).pack(fill="x", padx=16, pady=6)
        self._fields = ctk.CTkFrame(self, fg_color="transparent")
        self._fields.pack(fill="x", padx=16)
        ctk.CTkLabel(self, text="PREVIEW  (rainbow = LED wiring order)",
                     font=("Helvetica Neue", 9), text_color=DIM).pack(anchor="w", padx=16, pady=(8, 2))
        self._prev_cv = tk.Canvas(self, width=508, height=180, bg="#050505",
                                   highlightthickness=1, highlightbackground=BORDER)
        self._prev_cv.pack(padx=16)
        self._prev_id = self._prev_cv.create_image(0, 0, anchor="nw")
        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(br, text="SAVE", width=100,
                      fg_color=ACCENT, hover_color="#009966", text_color="#000",
                      font=MONO9, command=self._save).pack(side="right", padx=4)
        ctk.CTkButton(br, text="CANCEL", width=100,
                      fg_color=BG4, hover_color=BG3, border_color=BORDER,
                      border_width=1, text_color=DIM, font=MONO9,
                      command=self.destroy).pack(side="right")

    def _entry(self, parent, label, var):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=180, anchor="w",
                     font=MONO9, text_color=DIM).pack(side="left")
        ctk.CTkEntry(row, textvariable=var, width=120,
                     fg_color=BG3, border_color=BORDER,
                     text_color=TEXT, font=MONO9).pack(side="left")

    def _switch_type(self, t):
        for w in self._fields.winfo_children():
            w.destroy()
        self._vars = {}
        d = self._device.geometry
        if t == "linear":
            self._vars["led_count"] = ctk.StringVar(value=str(d.get("led_count", self._device.led_count)))
            self._entry(self._fields, "LED Count:", self._vars["led_count"])
        elif t == "snake":
            self._vars["segments"]         = ctk.StringVar(value=str(d.get("segments", 5)))
            self._vars["leds_per_segment"] = ctk.StringVar(value=str(d.get("leds_per_segment", 300)))
            self._vars["gap_px"]           = ctk.StringVar(value=str(d.get("gap_px", 15)))
            self._entry(self._fields, "Segments:",          self._vars["segments"])
            self._entry(self._fields, "LEDs per segment:",  self._vars["leds_per_segment"])
            self._entry(self._fields, "Gap between strips (px):", self._vars["gap_px"])
            ctk.CTkLabel(self._fields, text="Odd rows auto-reversed (snake wiring).",
                         font=("Helvetica Neue", 9), text_color=DIM).pack(anchor="w", pady=(4, 0))
        elif t == "globe":
            self._vars["led_count"]       = ctk.StringVar(value=str(d.get("led_count", 120)))
            self._vars["rotation_offset"] = ctk.StringVar(value=str(d.get("rotation_offset", 0)))
            self._vars["mapping"]         = ctk.StringVar(value=d.get("mapping", "sphere"))
            self._entry(self._fields, "LED Count:",           self._vars["led_count"])
            self._entry(self._fields, "Rotation offset (°):", self._vars["rotation_offset"])
            mr = ctk.CTkFrame(self._fields, fg_color="transparent")
            mr.pack(fill="x", pady=3)
            ctk.CTkLabel(mr, text="Mapping:", width=180, anchor="w",
                         font=MONO9, text_color=DIM).pack(side="left")
            ctk.CTkComboBox(mr, values=["sphere", "rings", "spiral"],
                            variable=self._vars["mapping"], width=120,
                            fg_color=BG3, border_color=BORDER, text_color=TEXT,
                            font=MONO9, button_color=BG4,
                            dropdown_fg_color=BG3).pack(side="left")
        self._update_preview()

    def _build_geom(self):
        t = self._type_var.get()
        def iv(k, d=0):
            try:    return int(self._vars[k].get())
            except: return d
        if t == "linear":  return LinearGeometry(iv("led_count", 300))
        if t == "snake":   return SnakeGeometry(iv("segments", 5), iv("leds_per_segment", 300), iv("gap_px", 15))
        if t == "globe":
            return GlobeGeometry(iv("led_count", 120), iv("rotation_offset", 0),
                                 self._vars.get("mapping", ctk.StringVar(value="sphere")).get())
        return LinearGeometry()

    def _update_preview(self):
        geom  = self._build_geom()
        arr   = rainbow_preview(geom.build_led_map(508, 180))
        photo = ImageTk.PhotoImage(Image.fromarray(arr))
        self._prev_cv.itemconfig(self._prev_id, image=photo)
        self._prev_photo = photo

    def _save(self):
        self._on_save(self._build_geom().to_dict())
        self.destroy()


class DeviceRow(ctk.CTkFrame):
    def __init__(self, master, device, idx, mgr, refresh_cb, geom_cb, **kw):
        super().__init__(master, fg_color=BG3, corner_radius=4, **kw)
        self.device  = device
        self._idx    = idx
        self._mgr    = mgr
        self._refresh = refresh_cb
        self._geom_cb = geom_cb

        row1 = ctk.CTkFrame(self, fg_color="transparent")
        row1.pack(fill="x", padx=4, pady=(4, 0))
        self._en = tk.BooleanVar(value=device.enabled)
        ctk.CTkCheckBox(row1, text="", variable=self._en,
                        width=16, height=16, checkbox_width=14, checkbox_height=14,
                        fg_color=ACCENT, border_color=BORDER,
                        command=self._toggle).pack(side="left", padx=2)
        self._dot = ctk.CTkLabel(row1, text="●", width=12,
                                  font=("Arial", 9), text_color=DIM)
        self._dot.pack(side="left")
        ctk.CTkLabel(row1, text=f"{device.name}\n{device.ip}",
                     font=("Helvetica Neue", 9), text_color=TEXT,
                     justify="left", anchor="w").pack(side="left", padx=4,
                                                       fill="x", expand=True)
        ctk.CTkButton(row1, text="✕", width=20, height=20,
                      fg_color=BG4, hover_color="#3a1010",
                      border_color=BORDER, border_width=1,
                      text_color="#cc4444",
                      command=self._remove).pack(side="right", padx=(0, 2))

        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.pack(fill="x", padx=4, pady=(0, 4))
        geom_type = device.geometry.get("type", "linear").upper()
        ctk.CTkButton(row2, text=f"⚙ LAYOUT [{geom_type}]",
                      height=20, corner_radius=3,
                      font=("Helvetica Neue", 9),
                      fg_color=BG4, hover_color=BG3,
                      border_color=BORDER, border_width=1, text_color=DIM,
                      command=self._layout).pack(side="left", padx=(0, 4),
                                                  fill="x", expand=True)
        ctk.CTkButton(row2, text="PING", width=40, height=20,
                      font=("Helvetica Neue", 9),
                      fg_color=BG4, hover_color=BG3,
                      border_color=BORDER, border_width=1, text_color=DIM,
                      command=self._ping).pack(side="right")

    def _toggle(self):
        self.device.enabled = self._en.get(); self._mgr.save()

    def _ping(self):
        self._dot.configure(text_color="#aaaa00")
        self._mgr.ping(self.device, lambda d: self.after(0, self._upd_dot))

    def _upd_dot(self):
        col = {"True": "#00cc44", "False": KILL_COL}.get(str(self.device.online), DIM)
        self._dot.configure(text_color=col)

    def _remove(self):
        self._mgr.remove(self._idx); self._refresh()

    def _layout(self):
        def _save(gd):
            self.device.geometry = gd; self._mgr.save(); self._geom_cb()
        GeometryEditorDialog(self, self.device, _save)


class EffectButton(ctk.CTkFrame):
    def __init__(self, master, effect_cls, on_select, **kw):
        kw.setdefault("height", 58)
        super().__init__(master, corner_radius=6,
                         fg_color=BG3, border_color=BORDER, border_width=1, **kw)
        self.pack_propagate(False)
        self.effect_cls = effect_cls
        self._on_select = on_select
        self._active    = False

        self._lbl = ctk.CTkLabel(self, text=effect_cls.name,
                                  font=ctk.CTkFont(size=8, weight="bold"),
                                  text_color=DIM, justify="center")
        self._lbl.place(relx=0.5, rely=0.40, anchor="center")

        self._strip = tk.Canvas(self, height=10, bg="#050505",
                                 highlightthickness=0, bd=0)
        self._strip.place(relx=0, rely=1.0, anchor="sw", relwidth=1.0, height=10)

        for w in (self, self._lbl, self._strip):
            w.bind("<Button-1>", self._click)

    def _click(self, _=None):
        self._on_select(self.effect_cls)

    def set_active(self, active: bool):
        self._active = active
        if active:
            self.configure(border_color=ACCENT, border_width=2, fg_color="#0d1f17")
            self._lbl.configure(text_color=ACCENT)
        else:
            self.configure(border_color=BORDER, border_width=1, fg_color=BG3)
            self._lbl.configure(text_color=DIM)

    def update_strip(self, px: np.ndarray):
        _draw_px_strip(self._strip, px, 12)


class EffectsPanel(ctk.CTkFrame):
    # 28 cat bar + 2 rows × (58 btn + 6 pad) = 28 + 128 = 156 + CTkFrame corner padding → 190
    def __init__(self, master, on_select, **kw):
        kw.setdefault("height", 190)
        super().__init__(master, fg_color=BG2, **kw)
        self.pack_propagate(False)
        self._on_select  = on_select
        self._buttons    = {}
        self._cat_btns   = []
        self._cat_frames = []
        self._active_cat  = 0
        self._active_cls  = None
        self._active_setup = "vault"  # updated by _switch_setup; gates Vault_ effects
        self._playing_cls = None
        self._build()

    def _build(self):
        cb = ctk.CTkFrame(self, fg_color=BG3, height=28)
        cb.pack(fill="x")
        cb.pack_propagate(False)
        for i, (name, _) in enumerate(CATEGORIES):
            cb.columnconfigure(i, weight=1)
            btn = ctk.CTkButton(cb, text=name, height=26,
                                font=ctk.CTkFont(size=9, weight="bold"),
                                fg_color="transparent", hover_color=BG4,
                                corner_radius=0, text_color=DIM,
                                command=lambda i=i: self._switch_cat(i))
            btn.grid(row=0, column=i, sticky="nsew", padx=1)
            self._cat_btns.append(btn)

        self._stack = ctk.CTkFrame(self, fg_color="transparent")
        self._stack.pack(fill="both", expand=True)

        for _, (_, effect_classes) in enumerate(CATEGORIES):
            frame = ctk.CTkFrame(self._stack, fg_color="transparent")
            n_cols = math.ceil(len(effect_classes) / 2)
            for c_i, cls in enumerate(effect_classes):
                row = c_i // n_cols
                col = c_i % n_cols
                btn = EffectButton(frame, cls, self._click)
                btn.grid(row=row, column=col, padx=3, pady=3, sticky="nsew")
                self._buttons[cls] = btn
            for c in range(n_cols):
                frame.columnconfigure(c, weight=1)
            frame.rowconfigure(0, weight=1)
            frame.rowconfigure(1, weight=1)
            self._cat_frames.append(frame)

        self._switch_cat(0)

    def _switch_cat(self, idx):
        for i, frame in enumerate(self._cat_frames):
            (frame.pack if i == idx else frame.pack_forget)(
                **({} if i != idx else {"fill": "both", "expand": True}))
        for i, btn in enumerate(self._cat_btns):
            btn.configure(text_color=ACCENT if i == idx else DIM,
                          fg_color=BG4 if i == idx else "transparent")
        self._active_cat = idx

    def _click(self, cls):
        if cls is self._active_cls:
            if btn := self._buttons.get(cls): btn.set_active(False)
            self._active_cls = None
            self._on_select(None)
            return
        if prev := self._buttons.get(self._active_cls): prev.set_active(False)
        self._active_cls = cls
        if btn := self._buttons.get(cls): btn.set_active(True)
        self._on_select(cls)

    def switch_cat(self, idx):         self._switch_cat(idx)
    def active_cls(self):              return self._active_cls
    def select_nth(self, n):
        _, effect_classes = CATEGORIES[self._active_cat]
        if 1 <= n <= len(effect_classes):
            self._click(effect_classes[n - 1])
    def update_strips(self, px):
        # Update whichever button is currently selected OR currently playing
        for cls in {self._active_cls, self._playing_cls}:
            if btn := self._buttons.get(cls):
                btn.update_strip(px)
    def set_playing(self, cls):
        """Highlight the currently playing effect badge and switch to its category tab."""
        if cls is self._playing_cls:
            return
        if prev := self._buttons.get(self._playing_cls):
            if self._playing_cls is not self._active_cls:
                prev.set_active(False)
        self._playing_cls = cls
        if cls is None:
            return
        # Switch to the category containing this effect
        for i, (_, effect_classes) in enumerate(CATEGORIES):
            if cls in effect_classes:
                if i != self._active_cat:
                    self._switch_cat(i)
                break
        if btn := self._buttons.get(cls):
            btn.set_active(True)

    def get_by_name(self, effect_name):
        """Find effect class by name string (case-insensitive). Returns class or None."""
        if not effect_name:
            return None
        name_lower = effect_name.lower()
        for _, pool in CATEGORIES:
            for cls in pool:
                if cls.__name__.lower() == name_lower:
                    return cls
        return None

    def get_available_for_phrase(self, phrase_type):
        """Return list of effect class names appropriate for this phrase type."""
        if not phrase_type:
            return []
        mapped = PHRASE_ALIAS.get(phrase_type, phrase_type)
        cat_dict = dict(CATEGORIES)
        pool = list(cat_dict.get(mapped.upper(), []))
        return [cls.__name__ for cls in pool]


class PaletteRow(ctk.CTkFrame):
    def __init__(self, master, palette, idx, select_cb, delete_cb, active, **kw):
        super().__init__(master,
                         fg_color="#0d1f17" if active else BG3, corner_radius=4,
                         border_color=ACCENT if active else BORDER,
                         border_width=2 if active else 1, **kw)
        cv = tk.Canvas(self, width=40, height=14, bg="#050505", highlightthickness=0)
        cv.pack(side="left", padx=6, pady=4)
        lut = palette.to_array(40)
        for i in range(40):
            r, g, b = int(lut[i, 0]), int(lut[i, 1]), int(lut[i, 2])
            cv.create_line(i, 0, i, 14, fill=f"#{r:02x}{g:02x}{b:02x}")
        del_btn = ctk.CTkButton(self, text="✕", width=20, height=20,
                                fg_color="transparent", hover_color="#3a1010",
                                text_color="#cc4444", font=ctk.CTkFont(size=10),
                                command=lambda: delete_cb(idx))
        del_btn.pack(side="right", padx=(0, 4))
        self._lbl = ctk.CTkLabel(self, text=palette.name,
                                 font=ctk.CTkFont(size=9, weight="bold"),
                                 text_color=ACCENT if active else TEXT, anchor="w")
        self._lbl.pack(side="left", fill="x", expand=True, pady=4)
        for w in (self, cv, self._lbl):
            w.bind("<Button-1>", lambda e: select_cb(idx))

    def set_active(self, active: bool):
        """Update highlight styling without rebuilding the widget."""
        self.configure(fg_color="#0d1f17" if active else BG3,
                       border_color=ACCENT if active else BORDER,
                       border_width=2 if active else 1)
        self._lbl.configure(text_color=ACCENT if active else TEXT)


class AddDeviceDialog(ctk.CTkToplevel):
    """Full LED install setup: name, IP, geometry type + params, preview, then SAVE."""

    def __init__(self, master, on_add):
        super().__init__(master)
        self.title("Add LED Install")
        self.geometry("580x560")
        self.resizable(False, False)
        self.configure(fg_color=BG2)
        self._on_add   = on_add
        self._type_var = ctk.StringVar(value="linear")
        self._vars     = {}
        self._build()
        self.after(50, self._bring_to_front)

    def _bring_to_front(self):
        self.lift(); self.focus_force(); self.grab_set()

    def _field(self, parent, label, default):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=170, anchor="w",
                     font=MONO9, text_color=DIM).pack(side="left")
        var = ctk.StringVar(value=str(default))
        ctk.CTkEntry(row, textvariable=var, width=140,
                     fg_color=BG3, border_color=BORDER,
                     text_color=TEXT, font=MONO9).pack(side="left")
        return var

    def _build(self):
        ctk.CTkLabel(self, text="ADD LED INSTALL",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=ACCENT).pack(pady=(14, 6))
        ctk.CTkFrame(self, height=1, fg_color=BORDER).pack(fill="x", padx=16, pady=(0, 10))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="x", padx=16)

        # ── Basic info ────────────────────────────────────────────────────────
        self._name_var = self._field(body, "Name:", "My WLED")
        self._ip_var   = self._field(body, "IP Address:", "192.168.1.16")

        # ── Geometry type ─────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color=BORDER).pack(fill="x", padx=16, pady=8)
        tr = ctk.CTkFrame(self, fg_color="transparent")
        tr.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(tr, text="Geometry:", font=MONO9, text_color=DIM,
                     width=110).pack(side="left")
        for t in ("linear", "snake", "globe"):
            ctk.CTkRadioButton(tr, text=t.upper(), variable=self._type_var, value=t,
                               font=MONO9, text_color=TEXT,
                               fg_color=ACCENT, hover_color=ACCENT,
                               command=self._switch_type).pack(side="left", padx=10)

        # ── Type-specific fields ───────────────────────────────────────────────
        self._fields = ctk.CTkFrame(self, fg_color="transparent")
        self._fields.pack(fill="x", padx=16)

        # ── Preview ───────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="PREVIEW  (rainbow = wiring order)",
                     font=("Helvetica Neue", 9), text_color=DIM).pack(anchor="w", padx=16, pady=(8, 2))
        self._prev_cv = tk.Canvas(self, width=548, height=130, bg="#050505",
                                   highlightthickness=1, highlightbackground=BORDER)
        self._prev_cv.pack(padx=16)
        self._prev_id = self._prev_cv.create_image(0, 0, anchor="nw")

        # ── Buttons ───────────────────────────────────────────────────────────
        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=16, pady=14)
        ctk.CTkButton(br, text="SAVE", width=120, height=34,
                      fg_color=ACCENT, hover_color="#009966",
                      text_color="#000",
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self._save).pack(side="right", padx=4)
        ctk.CTkButton(br, text="CANCEL", width=100, height=34,
                      fg_color=BG4, hover_color=BG3,
                      border_color=BORDER, border_width=1,
                      text_color=DIM, font=MONO9,
                      command=self.destroy).pack(side="right")

        self._switch_type()

    def _switch_type(self):
        for w in self._fields.winfo_children():
            w.destroy()
        self._type_vars = {}
        t = self._type_var.get()
        if t == "linear":
            self._type_vars["led_count"] = self._field(self._fields, "LED Count:", 300)
        elif t == "snake":
            self._type_vars["segments"]         = self._field(self._fields, "Segments:", 5)
            self._type_vars["leds_per_segment"] = self._field(self._fields, "LEDs per segment:", 300)
            self._type_vars["gap_px"]           = self._field(self._fields, "Gap between strips (px):", 15)
        elif t == "globe":
            self._type_vars["led_count"]       = self._field(self._fields, "LED Count:", 120)
            self._type_vars["rotation_offset"] = self._field(self._fields, "Rotation offset (°):", 0)
            mr = ctk.CTkFrame(self._fields, fg_color="transparent")
            mr.pack(fill="x", pady=3)
            ctk.CTkLabel(mr, text="Mapping:", width=170, anchor="w",
                         font=MONO9, text_color=DIM).pack(side="left")
            self._type_vars["mapping"] = ctk.StringVar(value="sphere")
            ctk.CTkComboBox(mr, values=["sphere", "rings", "spiral"],
                            variable=self._type_vars["mapping"], width=140,
                            fg_color=BG3, border_color=BORDER, text_color=TEXT,
                            font=MONO9, button_color=BG4,
                            dropdown_fg_color=BG3).pack(side="left")
        self._update_preview()

    def _build_geom(self):
        def iv(k, d=0):
            try:    return int(self._type_vars[k].get())
            except: return d
        t = self._type_var.get()
        if t == "snake":  return SnakeGeometry(iv("segments", 5), iv("leds_per_segment", 300), iv("gap_px", 15))
        if t == "globe":  return GlobeGeometry(iv("led_count", 120), iv("rotation_offset", 0),
                                               self._type_vars.get("mapping", ctk.StringVar(value="sphere")).get())
        return LinearGeometry(iv("led_count", 300))

    def _update_preview(self):
        try:
            geom  = self._build_geom()
            arr   = rainbow_preview(geom.build_led_map(548, 130))
            photo = ImageTk.PhotoImage(Image.fromarray(arr))
            self._prev_cv.itemconfig(self._prev_id, image=photo)
            self._prev_photo = photo
        except Exception:
            pass

    def _save(self):
        name = self._name_var.get().strip() or "WLED"
        ip   = self._ip_var.get().strip()
        geom = self._build_geom()
        self._on_add(name, ip, geom)
        self.destroy()


class PaletteEditorDialog(ctk.CTkToplevel):
    """Two-panel palette editor: list on left, stop editor on right with native color picker."""

    def __init__(self, master, mgr, refresh_cb):
        super().__init__(master)
        self.title("Palette Editor")
        self.geometry("680x500")
        self.minsize(600, 400)
        self.configure(fg_color=BG2)
        self._mgr     = mgr
        self._refresh = refresh_cb
        self._sel     = max(0, mgr._active)
        self.after(50, lambda: (self.lift(), self.focus_force(), self.grab_set()))
        self._build()
        self._select(self._sel)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        ctk.CTkLabel(self, text="PALETTE EDITOR",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=ACCENT).pack(pady=(12, 6))
        ctk.CTkFrame(self, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # ── Left: palette list ────────────────────────────────────────────────
        left = ctk.CTkFrame(body, width=200, fg_color=BG3, corner_radius=6)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ctk.CTkLabel(left, text="PALETTES", font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=ACCENT2).pack(anchor="w", padx=10, pady=(8, 4))

        self._pal_list = ctk.CTkScrollableFrame(left, fg_color="transparent",
                                                 scrollbar_button_color=BG4)
        self._pal_list.pack(fill="both", expand=True, padx=4)

        create_row = ctk.CTkFrame(left, fg_color="transparent")
        create_row.pack(fill="x", padx=6, pady=6)
        self._new_name = ctk.CTkEntry(create_row, placeholder_text="New palette…",
                                       fg_color=BG4, border_color=BORDER,
                                       text_color=TEXT, font=MONO9, height=28)
        self._new_name.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(create_row, text="+", width=28, height=28,
                      fg_color=BG4, hover_color=BG3,
                      border_color=ACCENT, border_width=1, text_color=ACCENT,
                      font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._create).pack(side="right")

        # ── Right: stop editor ────────────────────────────────────────────────
        self._right = ctk.CTkFrame(body, fg_color=BG2, corner_radius=6)
        self._right.pack(side="right", fill="both", expand=True, padx=(10, 0))

        # Preview bar at top of right panel
        prev_hdr = ctk.CTkFrame(self._right, fg_color="transparent")
        prev_hdr.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(prev_hdr, text="PREVIEW", font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=DIM).pack(side="left")
        self._prev_cv = tk.Canvas(self._right, height=22, bg="#050505",
                                   highlightthickness=1, highlightbackground=BORDER)
        self._prev_cv.pack(fill="x", padx=10, pady=(2, 6))

        ctk.CTkLabel(self._right, text="COLOR STOPS", font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=DIM).pack(anchor="w", padx=10)

        self._stops_sf = ctk.CTkScrollableFrame(self._right, fg_color="transparent",
                                                 scrollbar_button_color=BG4)
        self._stops_sf.pack(fill="both", expand=True, padx=8, pady=4)

        add_row = ctk.CTkFrame(self._right, fg_color="transparent")
        add_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkButton(add_row, text="+ ADD STOP", height=28,
                      fg_color=BG4, hover_color=BG3,
                      border_color=BORDER, border_width=1, text_color=DIM,
                      font=ctk.CTkFont(size=9, weight="bold"),
                      command=self._add_stop).pack(side="left", padx=(0, 6))
        self._pal_name_lbl = ctk.CTkLabel(add_row, text="", font=MONO9, text_color=ACCENT)
        self._pal_name_lbl.pack(side="right")

        self._populate_list()

    # ── Left panel helpers ────────────────────────────────────────────────────

    def _populate_list(self):
        for w in self._pal_list.winfo_children():
            w.destroy()
        for i, p in enumerate(self._mgr.palettes):
            is_sel = (i == self._sel)
            row = ctk.CTkFrame(self._pal_list,
                               fg_color="#0d1f17" if is_sel else BG4,
                               border_color=ACCENT if is_sel else "transparent",
                               border_width=1 if is_sel else 0,
                               corner_radius=4)
            row.pack(fill="x", pady=1)
            lbl = ctk.CTkLabel(row, text=p.name,
                               font=ctk.CTkFont(size=10, weight="bold") if is_sel else ctk.CTkFont(size=10),
                               text_color=ACCENT if is_sel else TEXT,
                               anchor="w")
            lbl.pack(side="left", fill="x", expand=True, padx=8, pady=4)
            ctk.CTkButton(row, text="✕", width=22, height=22,
                          fg_color="transparent", hover_color="#3a1010",
                          text_color="#cc4444", font=ctk.CTkFont(size=10),
                          command=lambda i=i: self._delete(i)).pack(side="right", padx=2)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, i=i: self._select(i))

    def _select(self, idx):
        if not self._mgr.palettes:
            return
        self._sel = max(0, min(idx, len(self._mgr.palettes) - 1))
        self._populate_list()
        self._populate_stops()

    # ── Right panel helpers ───────────────────────────────────────────────────

    def _populate_stops(self):
        for w in self._stops_sf.winfo_children():
            w.destroy()
        if not self._mgr.palettes:
            self._pal_name_lbl.configure(text="")
            return
        pal = self._mgr.palettes[self._sel]
        self._pal_name_lbl.configure(text=pal.name)
        for i, stop in enumerate(pal.stops):
            self._make_stop_row(i, stop, pal)
        self._update_preview()

    def _make_stop_row(self, i, stop, pal):
        hex_col = f"#{stop['r']:02x}{stop['g']:02x}{stop['b']:02x}"
        row = ctk.CTkFrame(self._stops_sf, fg_color=BG3, corner_radius=4)
        row.pack(fill="x", pady=2, padx=2)

        # Color swatch — click to open native color picker
        sw = tk.Canvas(row, width=32, height=24, bg=hex_col,
                       highlightthickness=2, highlightbackground=BORDER, cursor="hand2")
        sw.pack(side="left", padx=(6, 4), pady=4)
        sw.bind("<Button-1>", lambda e, i=i, pal=pal: self._pick_color(i, pal))

        # RGB hex label
        rgb_lbl = ctk.CTkLabel(row, text=hex_col.upper(),
                               font=ctk.CTkFont(family="Helvetica Neue", size=10, weight="bold"),
                               text_color=hex_col, width=68, anchor="w")
        rgb_lbl.pack(side="left", padx=4)

        # Position entry
        ctk.CTkLabel(row, text="pos", font=ctk.CTkFont(size=8), text_color=DIM).pack(side="left")
        pos_var = ctk.StringVar(value=f"{stop['pos']:.2f}")
        pos_e = ctk.CTkEntry(row, textvariable=pos_var, width=48, height=24,
                             fg_color=BG4, border_color=BORDER, text_color=TEXT,
                             font=ctk.CTkFont(size=9))
        pos_e.pack(side="left", padx=4)
        pos_e.bind("<FocusOut>", lambda e, i=i, v=pos_var, pal=pal: self._set_pos(i, v, pal))
        pos_e.bind("<Return>",   lambda e, i=i, v=pos_var, pal=pal: self._set_pos(i, v, pal))

        # Delete (only when >2 stops)
        if len(pal.stops) > 2:
            ctk.CTkButton(row, text="✕", width=22, height=22,
                          fg_color="transparent", hover_color="#3a1010",
                          text_color="#cc4444", font=ctk.CTkFont(size=10),
                          command=lambda i=i, pal=pal: self._del_stop(i, pal)
                          ).pack(side="right", padx=4)

    def _update_preview(self):
        if not self._mgr.palettes:
            return
        self._prev_cv.update_idletasks()
        w = self._prev_cv.winfo_width()
        if w < 4:
            return
        pal = self._mgr.palettes[self._sel]
        self._prev_cv.delete("all")
        for i in range(w):
            r, g, b = pal.color(i / max(1, w - 1))
            self._prev_cv.create_line(i, 0, i, 22, fill=f"#{r:02x}{g:02x}{b:02x}")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _pick_color(self, stop_idx, pal):
        from tkinter import colorchooser
        s    = pal.stops[stop_idx]
        init = f"#{s['r']:02x}{s['g']:02x}{s['b']:02x}"
        # Release grab so the native macOS color panel can receive events
        try:    self.grab_release()
        except Exception: pass
        result = colorchooser.askcolor(color=init, title="Pick color")
        try:    self.grab_set()
        except Exception: pass
        if result and result[0]:
            r, g, b = (int(x) for x in result[0])
            pal.stops[stop_idx].update(r=r, g=g, b=b)
            self._mgr.save()
            self._refresh()
            self._populate_stops()

    def _set_pos(self, stop_idx, var, pal):
        try:
            pos = max(0.0, min(1.0, float(var.get())))
            pal.stops[stop_idx]["pos"] = pos
            pal.stops.sort(key=lambda s: s["pos"])
            self._mgr.save()
            self._refresh()
            self._populate_stops()
        except ValueError:
            pass

    def _add_stop(self):
        if not self._mgr.palettes:
            return
        pal = self._mgr.palettes[self._sel]
        pal.stops.append({"pos": 0.5, "r": 128, "g": 128, "b": 128})
        pal.stops.sort(key=lambda s: s["pos"])
        self._mgr.save()
        self._refresh()
        self._populate_stops()

    def _del_stop(self, stop_idx, pal):
        if len(pal.stops) > 2:
            pal.stops.pop(stop_idx)
            self._mgr.save()
            self._refresh()
            self._populate_stops()

    def _create(self):
        name = self._new_name.get().strip() or "Custom"
        self._mgr.add(name, [{"pos": 0.0, "r": 0, "g": 0, "b": 0},
                               {"pos": 1.0, "r": 255, "g": 255, "b": 255}])
        self._new_name.delete(0, "end")
        self._sel = len(self._mgr.palettes) - 1
        self._populate_list()
        self._populate_stops()
        self._refresh()

    def _delete(self, idx):
        self._mgr.remove(idx)
        self._sel = max(0, min(self._sel, len(self._mgr.palettes) - 1))
        self._populate_list()
        self._populate_stops()
        self._refresh()


# ══════════════════════════════════════════════════════════════════════════════
# Main application
# ══════════════════════════════════════════════════════════════════════════════

# ── Demo Player — simuleert CDJ zonder hardware ──────────────────────────────
class _DemoPlayer:
    """Drives demo mode from tch_demo_plan_v2 (demo_plan.json).

    All 12 phrases (intro→verse→down→up→chorus×2→down→verse→up×2→chorus→outro)
    compressed to ≤60 seconds at 125 BPM (120 beats = 57.6 s).

    Usage:
        dp = _DemoPlayer()
        snap = dp.tick()          # call every frame
        cm.apply_plan(1, dp.choreo_plan)  # load plan into ChoreographyManager
    """

    _PLAN_PATH = os.path.join(os.path.dirname(__file__), "demo_plan.json")

    # Compressed beat counts for each of the 12 phrases (total = 120 beats)
    # intro, verse, down, up, chorus, up, chorus, down, verse, up, chorus, outro
    _COMPRESSED_BEATS = [8, 12, 8, 8, 16, 8, 12, 8, 8, 8, 16, 8]

    def __init__(self, bpm: float = 125.0) -> None:
        self.bpm       = float(bpm)
        self._start    = time.perf_counter()
        self._beat_dur = 60.0 / bpm

        # Load plan JSON
        try:
            with open(self._PLAN_PATH, encoding="utf-8") as _f:
                _raw = json.load(_f)
            _entries = _raw.get("plan", [])
        except Exception as _e:
            _log.warning("_DemoPlayer: could not load demo_plan.json: %s", _e)
            _entries = []

        # Build compressed phrase table: (beat_start, beat_end, kind, plan_entry)
        self._phrases: list[tuple] = []
        beat = 1
        for i, n_beats in enumerate(self._COMPRESSED_BEATS):
            if i < len(_entries):
                raw_e = _entries[i]
                kind  = raw_e.get("kind", "verse")
                orig_dur = max(1, raw_e.get("beat_end", 1) - raw_e.get("beat_start", 1))
                scale    = n_beats / orig_dur

                # Build plan entry with compressed beat_start
                pe = {k: v for k, v in raw_e.items()
                      if k not in ("beat_end", "phrase_index", "_comment", "mid_phrase_changes")}
                pe["beat_start"] = beat

                # Scale mid_phrase_changes at_beat proportionally
                scaled_mpc = []
                for mc in raw_e.get("mid_phrase_changes", []):
                    if "at_beat" not in mc:
                        continue
                    new_at = max(1, round(mc["at_beat"] * scale))
                    if new_at < n_beats:       # only if it fits in the compressed phrase
                        mc_s = dict(mc)
                        mc_s["at_beat"] = new_at
                        scaled_mpc.append(mc_s)
                pe["mid_phrase_changes"] = scaled_mpc
            else:
                kind = "verse"
                pe   = {"kind": kind, "beat_start": beat}

            self._phrases.append((beat, beat + n_beats, kind, pe))
            beat += n_beats

        self._total_beats = beat - 1

        # Choreo plan dict: phrase_idx → entry (ChoreographyManager.apply_plan format)
        self.choreo_plan: dict = {i: pe for i, (_, _, _, pe) in enumerate(self._phrases)}

    def tick(self):
        """Return a fake PlayerSnapshot for the current demo position."""
        elapsed   = time.perf_counter() - self._start
        total_dur = self._total_beats * self._beat_dur
        elapsed   = elapsed % total_dur

        beat_float  = elapsed / self._beat_dur + 1.0
        beat_n      = int(beat_float)
        beat_in_bar = ((beat_n - 1) % 4) + 1
        pos_ms      = elapsed * 1000.0

        # Find active phrase
        cur_kind, cur_bs, cur_be, cur_idx = "intro", 1, self._total_beats, 0
        for idx, (bs, be, kind, _) in enumerate(self._phrases):
            if bs <= beat_n < be:
                cur_kind, cur_bs, cur_be, cur_idx = kind, bs, be, idx
                break

        class _FakePhrase:
            pass
        fp = _FakePhrase()
        fp.kind       = cur_kind
        fp.beat_start = cur_bs
        fp.beat_end   = cur_be
        fp.fill       = False
        fp.fill_beat  = 0
        fp.index      = cur_idx
        fp.display    = cur_kind.upper()

        class _FakeSnap:
            pass
        s = _FakeSnap()
        s.bpm             = self.bpm
        s.track_id        = 1
        s.beat_number     = beat_n
        s.beat_within_bar = beat_in_bar
        s.position_ms     = pos_ms
        s.is_playing      = True
        s.current_phrase  = fp
        s.phrases         = tuple(
            type("P", (), {"kind": k, "beat_start": bs, "beat_end": be,
                           "fill": False, "fill_beat": 0,
                           "index": i, "display": k.upper()})()
            for i, (bs, be, k, _) in enumerate(self._phrases)
        )
        s.cues        = ()
        s.bass_energy = 0.3 + 0.4 * abs(math.sin(elapsed * 0.7))
        s.mid_energy  = 0.2 + 0.3 * abs(math.sin(elapsed * 1.3))
        s.treble_energy = 0.1 + 0.2 * abs(math.sin(elapsed * 2.1))
        return s


class DJLightsApp(ctk.CTk):
    FPS    = max(10, min(120, int(_settings.get("render.fps", 44))))
    N_LEDS = 1500
    _PREFS_PATH = os.path.join(os.path.dirname(__file__), "djlights_prefs.json")

    def __init__(self):
        super().__init__()

        # macOS system priority — start onmiddellijk zodat App Nap uitgeschakeld is
        # vóór de render-thread opstaat.  Wordt ook gestart vanuit __main__ (zie onder),
        # maar deze aanroep dekt het geval dat de app als module wordt geïmporteerd.
        try:
            from system_priority import SystemPriority
            import sys as _sys
            _existing = getattr(_sys.modules.get("__main__"), "_system_priority", None)
            if _existing is None:
                self._system_priority = SystemPriority()
                self._system_priority.start()
                _sys.modules["__main__"]._system_priority = self._system_priority
            else:
                self._system_priority = _existing
            _log.info("[DJLightsApp] %s", self._system_priority.status_line())
        except Exception as _sp_e:
            _log.warning("[DJLightsApp] SystemPriority niet beschikbaar: %s", _sp_e)
            self._system_priority = None

        self.title("djlights")
        self.geometry("1340x860")
        self.minsize(1100, 700)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Window icon
        try:
            _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
            _ico = ImageTk.PhotoImage(Image.open(_icon_path).resize((64, 64), Image.LANCZOS))
            self.iconphoto(True, _ico)
            self._ico_ref = _ico
        except Exception:
            pass

        self._device_mgr  = DeviceManager()
        self._palette_mgr = PaletteManager()
        self._sender      = DDPSender()

        # Beta: globe renderer (additive — only used for globe devices with a globe plan)
        try:
            from globe_renderer import GlobeRenderer
            self._globe_renderer = GlobeRenderer(n_leds=120)
        except Exception as _gre:
            self._globe_renderer = None
            _log.warning("Globe renderer unavailable: %s", _gre)

        # Cave strip renderer — energy reinforcement only on up/chorus
        try:
            from cave_strip_renderer import CaveStripRenderer
            self._cave_strip_renderer = CaveStripRenderer(n_leds=768)
        except Exception as _csr:
            self._cave_strip_renderer = None
            _log.warning("Cave strip renderer unavailable: %s", _csr)
        self._cs_env             = 0.0   # smoothed energy envelope driving back strip gate
        self._cs_env_t           = time.perf_counter()
        self._last_back_px       = np.zeros((768, 3), dtype=np.uint8)  # preview cache

        # Beta: vault generative renderer (additive — only used for non-globe devices)
        try:
            from vault_renderer import VaultRenderer
            self._vault_renderer = VaultRenderer()
        except Exception:
            self._vault_renderer = None
        self._vault_gen_enabled = False
        self._vault_forced_pattern = None   # set when a vault effect button is selected
        self._manual_grouping = False       # True when user has manually set grouping
        self._manual_spacing  = False       # True when user has manually set spacing
        self._manual_palette  = False       # True when user has manually picked a palette

        # Advanced persistent-buffer engine (DDPCanvas / particle system)
        try:
            from advanced_engine import AdvancedRenderer
            self._adv_renderer = AdvancedRenderer()
        except Exception:
            self._adv_renderer = None
        self._adv_gen_enabled     = False
        self._adv_forced_pattern  = None   # set when an advanced effect button is selected

        self._start_time  = time.perf_counter()
        self._running     = True
        self._kill        = False
        self._demo_mode   = False    # ChoreographyManager demo: simuleert CDJ zonder hardware
        self._demo_player = None     # _DemoPlayer instance when demo is active
        self._active_inst  = None
        self._active_choreo_mode_cls = None   # set when user clicks a CHOREO tab button
        self._ai_direction     = "forward"        # "forward" | "reverse" | "mirror"
        self._ai_grouping      = 1                # consecutive lit LEDs per group (1–10)
        self._ai_spacing       = 0                # dark LEDs between groups (0–30)
        self._ai_offset        = 0.0              # phase shift 0.0–1.0
        self._ai_density       = 1.0              # 0.2–1.0 fraction of LEDs lit
        self._ai_active_strips = [1, 2, 3, 4, 5] # which strips are lit (1-based)
        # BLT auto-effect: changes on phrase transition
        self._blt_phrase_inst      = None   # current effect instance
        self._blt_phrase_last      = ""     # last phrase kind seen
        self._blt_phrase_idx_last  = -1    # last phrase_idx for which effect was applied
        self._ai_plan_applied      = set() # (track_id, phrase_idx) that got EFFECT_FROM_PLAN
        self._blt_player_last      = 0      # player driving effects
        self._blt_effect_beat      = 0      # beat_number when current effect started
        self._blt_last_beat_n      = 0      # for cue-press detection (beat jump)
        self._blt_phrase_change_bn = -99   # beat_n when last phrase_changed fired (suppresses cue_press)
        self._blt_reroll_flag      = False  # set by OSC callback to force reroll next frame
        self._blt_reroll_time      = 0.0   # perf_counter when reroll flag was set (for sync log)
        self._ddp_log_pending      = False  # True after phrase change; cleared after first DDP send
        self._blt_committed_player = 0      # player committed by UI hysteresis (0 = auto)
        self._deck_events          = []     # [(perf_t, player, event_type, detail_str), ...]
        self._sb_last_pin          = -1     # cached pin for status bar flicker prevention
        self._sb_snap_cache        = None   # last snap seen by render thread — read by UI thread
        self._ai_active_flag       = True   # set by AI toggle, read by render thread safely
        self._pre_chorus_dark            = False  # True during last 4 beats of "up" before chorus
        self._chorus_impact_active       = False  # True during first 4 beats of chorus
        self._chorus_impact_pending_inst = None   # saved real effect for after impact
        self._chorus_impact_fire_t       = None   # perf_counter time to pre-arm fire
        self._chorus_impact_pre_armed    = False  # prevents re-arming each frame
        self._up_ramp_base_speed         = 0.9    # speed at start of current "up" phrase
        self._up_ramp_base_bright        = 0.55   # brightness at start of current "up" phrase
        self._sb_last_deck         = None
        self._sb_last_track        = None
        self._sb_last_phrase       = None
        self._sb_last_mode         = None
        self._sb_last_effect       = None
        self._last_px     = np.zeros((1, 3), dtype=np.uint8)
        self._last_px_devs = []          # list of (dev, px_dev) per-device arrays
        self._px_lock     = threading.Lock()

        # ChoreographyManager — ambient noise preview when no manual effect is active.
        # Wires block masking, strip patterns and phrase inertia into the strip preview.
        try:
            from choreography_manager import ChoreographyManager
            self._choreo_mgr = ChoreographyManager(n_leds=self.N_LEDS)
        except Exception as _cm_err:
            _log.warning("ChoreographyManager niet beschikbaar: %s", _cm_err)
            self._choreo_mgr = None
        self._tap_times   = []

        # AI light control state
        self._last_bar_t              = 0.0    # perf_counter when last beat-1 fired
        self._last_bar_beat_n         = -1     # beat_number of that bar start (debounce)
        self._last_beat_t             = 0.0    # perf_counter when last beat fired
        self._last_beat_n             = -1     # beat_number debounce
        self._last_2beat_t         = 0.0    # perf_counter when last odd beat (1 or 3) fired
        self._last_2beat_beat_n    = -1     # beat_number debounce for 2beat
        self._ai_beat_overlay         = "bar"  # "bar" = brightness | "beat"/"2beat" = sparks | "off"
        self._ai_render_mode          = "tile" # "full" | "tile" | "cascade" — tile default keeps random mode clean
        self._sparks                  = []     # list of (positions, r, g, b, trigger_time)
        self._sparks_trigger_n        = -1     # debounce: last beat_number that fired sparks
        self._session_memory_path     = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "session_memory.json")
        self._phrase_history          = self._load_session_memory()
        self._effect_start_time   = time.perf_counter()
        self._current_effect_name = None
        self._last_loop_state     = False
        self._loop_length         = None   # detected loop size in beats
        self._loop_decision_beat  = None   # beat_in_phrase when last loop decision was made
        self._ai_wv_tids          = {}     # player_num → track_id last sent for waveform analysis
        self._ai_wv_urls          = {}     # player_num → waveform_url last sent
        self._ai_plan_tids        = {}     # player_num → track_id last sent for planning
        self._ai_phrases_pending  = {}     # player_num → (track_id, first_seen_time) waiting for phrases
        self._ai_phrases_have     = {}     # player_num → track_id for which phrases were last seen
        self._ai_phrases_logged   = {}     # player_num → set of milestone seconds already logged
        self._ai_plan_fail_times  = {}     # track_id → time.time() of last failure (retry backoff)
        self._mid_phrase_applied  = set()  # (track_id, phrase_idx, at_beat) guard

        # Timing / analysis log
        _tlog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  f"timing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        self._tlog = open(_tlog_path, "w", buffering=1)   # line-buffered
        self._tlog_t0    = time.perf_counter()            # session start reference
        self._phrase_detect_t = 0.0                       # perf_counter when last phrase change detected
        self._tlog.write("# djlights timing log — " + datetime.now().isoformat() + "\n")
        self._tlog.write("# cols: HH:MM:SS.mmm | elapsed_ms | event | detail\n")
        self._tlog.write(f"# app log: {_app_log_path}\n")
        _log.info("Timing log: %s", _tlog_path)
        _log.info("App log:    %s", _app_log_path)

        # Canvas state
        self._led_bands  = []
        self._canvas_w   = 1
        self._canvas_h   = 1
        self._canvas_photo = None

        # Master controls
        self._bpm_var          = ctk.DoubleVar(value=128.0)
        self._bright_var       = ctk.DoubleVar(value=1.0)
        self._speed_var        = ctk.DoubleVar(value=1.0)
        self._master_bright_var = ctk.DoubleVar(value=1.0)

        # ProDJ (waveforms only) + BLT (player state)
        self._prodj         = None
        self._blt           = None
        self._cdj_last_tid  = {}   # player_number → last track_id seen by _on_cdj_change
        self._bridge_restarting = False

        self._build_ui()
        self._bind_keys()
        self._load_prefs()

        def _render_thread_exc_hook(args):
            _log.error("RENDER_THREAD_CRASH: %s", args.exc_value, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            self._tlog_write("RENDER_THREAD_CRASH", f"{args.exc_type.__name__}: {args.exc_value}")
        _rt = threading.Thread(target=self._render_loop, daemon=True, name="render")
        _rt.start()
        threading.excepthook = _render_thread_exc_hook
        # BLT and prodj both claim a Virtual CDJ slot — they conflict.
        # When BLT is available it is the primary source; skip prodj to avoid port conflict.
        if PRODJ_OK and not BLT_OK:
            threading.Thread(target=self._start_prodj, daemon=True).start()
        if BLT_OK:
            threading.Thread(target=self._start_blt, daemon=True).start()
        # Auto-launch BLT app if not already running
        try:
            import subprocess as _sp
            _sp.Popen(["open", "-a", "Beat Link Trigger"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        self._schedule_ui_refresh()
        # Immediately ping all devices so status dot shows on startup
        self.after(500, lambda: [
            self._device_mgr.ping(d, callback=lambda _d: self.after(0, self._update_wled_dot))
            for d in self._device_mgr.enabled_devices()
        ])

        # Mobile feedback server — bad vote triggers immediate reroll
        try:
            from web_feedback import FeedbackServer
            self._feedback = FeedbackServer(
                on_bad_vote=lambda: setattr(self, "_blt_reroll_flag", True),
                port=int(_settings.get("network.feedback_port", 5050)))
            self._feedback.start()
        except Exception as e:
            _log.warning("Feedback server not started: %s", e)
            self._feedback = None

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Title bar
        title_bar = ctk.CTkFrame(self, height=36, fg_color=BG2, corner_radius=0)
        title_bar.pack(fill="x", side="top")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(title_bar, text="  DJLIGHTS",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=ACCENT).pack(side="left", padx=4)
        self._kill_btn = ctk.CTkButton(title_bar, text="⬛  KILL ALL",
                                        width=112, height=26,
                                        font=ctk.CTkFont(size=10, weight="bold"),
                                        fg_color=KILL_COL, hover_color="#991122",
                                        text_color="#fff",
                                        command=self._toggle_kill)
        self._kill_btn.pack(side="right", padx=8, pady=5)

        self._demo_btn = ctk.CTkButton(title_bar, text="▶  DEMO",
                                        width=90, height=26,
                                        font=ctk.CTkFont(size=10, weight="bold"),
                                        fg_color=BG3, hover_color=ACCENT2,
                                        text_color=DIM,
                                        command=self._toggle_demo)
        self._demo_btn.pack(side="right", padx=4, pady=5)

        # ── Status bar (deck / track / AI mode / effect) ──────────────────────
        sb = ctk.CTkFrame(self, height=28, fg_color=BG3, corner_radius=0)
        sb.pack(fill="x", side="top")
        sb.pack_propagate(False)

        self._sb_deck = ctk.CTkLabel(sb, text="LED: —",
                                      font=ctk.CTkFont(size=11, weight="bold"),
                                      text_color=ACCENT, width=60)
        self._sb_deck.pack(side="left", padx=(8, 4))

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_track = ctk.CTkLabel(sb, text="no track",
                                       font=ctk.CTkFont(size=11),
                                       text_color=TEXT, anchor="w", width=180)
        self._sb_track.pack(side="left", padx=4)

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_phrase = ctk.CTkLabel(sb, text="—",
                                        font=ctk.CTkFont(size=11, weight="bold"),
                                        text_color=ACCENT2, width=80)
        self._sb_phrase.pack(side="left", padx=4)

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_mode = ctk.CTkLabel(sb, text="RANDOM",
                                      font=ctk.CTkFont(size=11, weight="bold"),
                                      text_color=DIM, width=56)
        self._sb_mode.pack(side="left", padx=4)

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        def _cycle_render_mode():
            modes = ["full", "tile", "cascade"]
            self._ai_render_mode = modes[(modes.index(self._ai_render_mode) + 1) % 3]
            self._sb_render_btn.configure(text=self._ai_render_mode.upper())

        self._sb_render_btn = ctk.CTkButton(
            sb, text="FULL", width=64, height=20,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=BG3, hover_color=BG4,
            border_color=BORDER, border_width=1,
            command=_cycle_render_mode)
        self._sb_render_btn.pack(side="left", padx=4)

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_effect = ctk.CTkLabel(sb, text="—",
                                        font=ctk.CTkFont(size=11),
                                        text_color=DIM, anchor="w", width=90)
        self._sb_effect.pack(side="left", padx=4)

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_palette_lbl = ctk.CTkLabel(sb, text="—",
                                             font=ctk.CTkFont(size=10, weight="bold"),
                                             text_color=ACCENT2, anchor="w", width=80)
        self._sb_palette_lbl.pack(side="left", padx=4)
        self._sb_last_palette = None

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        # Live effect parameters: choreo strip state or AI params
        self._sb_params = ctk.CTkLabel(sb, text="",
                                        font=ctk.CTkFont(size=10),
                                        text_color=DIM, anchor="w", width=400)
        self._sb_params.pack(side="left", padx=4)
        self._sb_last_params = ""

        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="left", padx=2)

        self._sb_wled = ctk.CTkLabel(sb, text="● WLED",
                                      font=ctk.CTkFont(size=10, weight="bold"),
                                      text_color="gray", width=64)
        self._sb_wled.pack(side="left", padx=4)
        self._sb_wled_last = None   # True/False/None — prevents redundant redraws
        self._last_ping_t = 0.0     # time of last auto-ping

        # Deck pin buttons — right side of status bar, always visible
        ctk.CTkLabel(sb, text="|", text_color=BORDER).pack(side="right", padx=2)
        self._sb_pin_btns = {}
        for label, val in reversed([("AUTO", 0), ("P1", 1), ("P2", 2), ("P3", 3), ("P4", 4)]):
            b = ctk.CTkButton(sb, text=label, width=36, height=20,
                              font=ctk.CTkFont(size=10, weight="bold"),
                              fg_color=ACCENT if val == 0 else BG3,
                              hover_color=BG4,
                              border_color=BORDER, border_width=1,
                              text_color="#000000" if val == 0 else DIM,
                              command=lambda v=val: self._sb_set_pin(v))
            b.pack(side="right", padx=1, pady=4)
            self._sb_pin_btns[val] = b

        # Tabview
        self._tabview = ctk.CTkTabview(self,
                                        fg_color=BG,
                                        segmented_button_fg_color=BG2,
                                        segmented_button_selected_color=ACCENT,
                                        segmented_button_selected_hover_color=ACCENT,
                                        segmented_button_unselected_color=BG2,
                                        segmented_button_unselected_hover_color=BG3,
                                        text_color=TEXT,
                                        text_color_disabled=DIM,
                                        border_color=BORDER, border_width=1)
        self._tabview.pack(fill="both", expand=True)

        # ── CDJ MONITOR tab ───────────────────────────────────────────────────
        cdj_tab = self._tabview.add("CDJ MONITOR")
        cdj_tab.configure(fg_color="transparent")
        self._cdj_tab = CDJTab(cdj_tab, px_getter=self._get_last_px,
                               restart_cb=self._restart_bridge if PRODJ_OK else None,
                               ai_getter=lambda: getattr(self, "_ai_control", None))
        self._cdj_tab.pack(fill="both", expand=True)


        # ── LED CONTROL tab ───────────────────────────────────────────────────
        led_tab = self._tabview.add("LED CONTROL")
        led_tab.configure(fg_color="transparent")
        self._build_led_tab(led_tab)

        # ── AI CONTROL tab ────────────────────────────────────────────────────
        ai_tab = self._tabview.add("AI CONTROL")
        ai_tab.configure(fg_color="transparent")
        from ai_light_control import AILightControl
        self._ai_control = AILightControl(
            master=ai_tab,
            wled_manager=self._sender,
            palette_manager=self._palette_mgr,
            effect_manager=self._effects_panel,
            on_active_change=lambda v: setattr(self, "_ai_active_flag", v),
        )
        self._ai_control.pack(fill="both", expand=True)
        # Push plan updates to CDJ Monitor plan panels
        self._ai_control.plan_notify_cb = lambda tid: self.after(
            0, lambda t=tid: self._cdj_tab.refresh_plan(t))


        # ── SETTINGS tab ──────────────────────────────────────────────────────
        settings_tab = self._tabview.add("SETTINGS")
        settings_tab.configure(fg_color="transparent")
        from settings_tab import SettingsTab
        self._settings_tab = SettingsTab(
            settings_tab,
            palette_names_getter=lambda: [getattr(p, "name", "") for p in self._palette_mgr.palettes],
            on_phrase_settings_changed=apply_phrase_settings,
        )
        self._settings_tab.pack(fill="both", expand=True)

        self._mixer_midi = None  # MIDI tab disabled — re-enable later

    def _build_led_tab(self, tab):
        self._build_bottom_bar(tab)
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self._build_left_panel(body)
        self._build_center_panel(body)
        self._build_right_panel(body)

    def _build_left_panel(self, parent):
        pane = ctk.CTkFrame(parent, width=215, fg_color=BG2, corner_radius=0)
        pane.pack(side="left", fill="y")
        pane.pack_propagate(False)

        # ── Setup selector ────────────────────────────────────────────────────
        setup_row = ctk.CTkFrame(pane, fg_color="transparent")
        setup_row.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(setup_row, text="SETUP",
                     font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=DIM).pack(side="left")
        self._setup_var = ctk.StringVar(
            value=self._device_mgr.active_setup.upper())
        self._setup_menu = ctk.CTkOptionMenu(
            setup_row,
            variable=self._setup_var,
            values=[n.upper() for n in self._device_mgr.setup_names()],
            width=108, height=22,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color=BG4, button_color=BG3, button_hover_color=BORDER,
            text_color=ACCENT,
            command=self._switch_setup,
        )
        self._setup_menu.pack(side="right")
        ctk.CTkFrame(pane, height=1, fg_color=BORDER).pack(fill="x", padx=10, pady=(4, 2))

        # ── Devices ───────────────────────────────────────────────────────────
        ctk.CTkLabel(pane, text="DEVICES",
                     font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=ACCENT2).pack(anchor="w", padx=10, pady=(2, 2))
        ctk.CTkFrame(pane, height=1, fg_color=BORDER).pack(fill="x", padx=10, pady=(0, 4))
        self._device_frame = ctk.CTkScrollableFrame(pane, fg_color="transparent",
                                                     scrollbar_button_color=BG4)
        self._device_frame.pack(fill="both", expand=True, padx=6)
        ctk.CTkButton(pane, text="+ ADD DEVICE", height=26, corner_radius=4,
                      font=ctk.CTkFont(size=9, weight="bold"),
                      fg_color=BG4, hover_color=BG3,
                      border_color=ACCENT2, border_width=1, text_color=ACCENT2,
                      command=self._add_device).pack(fill="x", padx=10, pady=6)
        self._refresh_devices()


    def _build_center_panel(self, parent):
        pane = ctk.CTkFrame(parent, fg_color="transparent")
        pane.pack(side="left", fill="both", expand=True)

        # Effects panel FIRST to claim bottom space before canvas gets expand
        self._effects_panel = EffectsPanel(pane, on_select=self._select_effect)
        self._effects_panel._active_setup = self._device_mgr.active_setup
        self._effects_panel.pack(fill="x", side="bottom")

        # Canvas fills rest
        canvas_frame = ctk.CTkFrame(pane, fg_color="#050505", corner_radius=0, height=280)
        canvas_frame.pack(fill="x", expand=False)
        canvas_frame.pack_propagate(False)
        self._led_canvas = tk.Canvas(canvas_frame, bg="#0a0a0a",
                                      highlightthickness=0, bd=0)
        self._led_canvas.pack(fill="both", expand=True)
        self._canvas_img_id = self._led_canvas.create_image(0, 0, anchor="nw")
        self._led_canvas.bind("<Configure>", self._on_canvas_configure)

    def _build_right_panel(self, parent):
        pane = ctk.CTkFrame(parent, width=205, fg_color=BG2, corner_radius=0)
        pane.pack(side="right", fill="y")
        pane.pack_propagate(False)
        ctk.CTkLabel(pane, text="PALETTES",
                     font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=ACCENT2).pack(anchor="w", padx=10, pady=(8, 2))
        ctk.CTkFrame(pane, height=1, fg_color=BORDER).pack(fill="x", padx=10, pady=(0, 4))
        self._palette_frame = ctk.CTkScrollableFrame(pane, fg_color="transparent",
                                                      scrollbar_button_color=BG4)
        self._palette_frame.pack(fill="both", expand=True, padx=6)
        btn_row = ctk.CTkFrame(pane, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=6)
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        self._rand_btn = ctk.CTkButton(btn_row, text="RANDOMIZE", height=26, corner_radius=4,
                      font=ctk.CTkFont(size=9, weight="bold"),
                      fg_color=BG4, hover_color=BG3,
                      border_color=ACCENT, border_width=1, text_color=ACCENT,
                      command=self._toggle_randomize)
        self._rand_btn.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self._randomizing = False
        ctk.CTkButton(btn_row, text="EDIT", height=26, corner_radius=4,
                      font=ctk.CTkFont(size=9, weight="bold"),
                      fg_color=BG4, hover_color=BG3,
                      border_color=ACCENT2, border_width=1, text_color=ACCENT2,
                      command=self._open_palette_editor).grid(row=0, column=1, sticky="ew", padx=(2, 0))
        self._refresh_palettes()

    def _build_bottom_bar(self, parent):
        bar = ctk.CTkFrame(parent, height=60, fg_color=BG2, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        bf = ctk.CTkFrame(bar, fg_color="transparent")
        bf.pack(side="left", padx=(12, 0), pady=8)
        ctk.CTkLabel(bf, text="BPM", font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        bpm_r = ctk.CTkFrame(bf, fg_color="transparent"); bpm_r.pack()
        self._bpm_str = ctk.StringVar(value=str(int(self._bpm_var.get())))
        bpm_entry = ctk.CTkEntry(bpm_r, textvariable=self._bpm_str, width=62, height=26,
                     font=ctk.CTkFont(size=12, weight="bold"),
                     fg_color=BG3, border_color=BORDER, text_color=ACCENT)
        bpm_entry.pack(side="left", padx=(0, 4))

        def _commit_bpm(*_):
            try:
                v = float(self._bpm_str.get())
                if 20 <= v <= 400:
                    self._bpm_var.set(v)
                    self._bpm_str.set(str(round(v, 1)))
            except ValueError:
                self._bpm_str.set(str(round(self._bpm_var.get(), 1)))

        bpm_entry.bind("<Return>", _commit_bpm)
        bpm_entry.bind("<FocusOut>", _commit_bpm)

        ctk.CTkButton(bpm_r, text="TAP", width=36, height=26,
                      font=ctk.CTkFont(size=9, weight="bold"),
                      fg_color=BG4, hover_color=BG3,
                      border_color=BORDER, border_width=1, text_color=DIM,
                      command=self._tap_bpm).pack(side="left")

        def _sl(parent, label, var, from_, to, w=130):
            f = ctk.CTkFrame(parent, fg_color="transparent"); f.pack(side="left", padx=10, pady=8)
            hdr = ctk.CTkFrame(f, fg_color="transparent"); hdr.pack(fill="x")
            ctk.CTkLabel(hdr, text=label, font=("Helvetica Neue", 8), text_color=DIM).pack(side="left")
            vl = ctk.CTkLabel(hdr, text=f"{var.get():.2f}", font=("Helvetica Neue", 8),
                               text_color=TEXT, width=32)
            vl.pack(side="right")
            ctk.CTkSlider(f, variable=var, from_=from_, to=to, width=w, height=14,
                          button_color=ACCENT, button_hover_color=ACCENT,
                          progress_color=BG4, fg_color=BORDER,
                          command=lambda v: vl.configure(text=f"{float(v):.2f}")
                          ).pack()

        _sl(bar, "BRIGHT", self._bright_var, 0.0, 1.0, 130)
        _sl(bar, "MASTER", self._master_bright_var, 0.0, 1.0, 130)

        # Speed: beat-aligned values only — free values break beat-sync
        _speed_frame = ctk.CTkFrame(bar, fg_color="transparent")
        _speed_frame.pack(side="left", padx=10, pady=8)
        ctk.CTkLabel(_speed_frame, text="SPEED", font=("Helvetica Neue", 8),
                     text_color=DIM).pack(anchor="w")
        _speed_seg = ctk.CTkSegmentedButton(
            _speed_frame, values=["0.5×", "1×", "2×"], width=110, height=22,
            font=ctk.CTkFont(size=9, weight="bold"),
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=BG4, unselected_hover_color=BG3,
            text_color=TEXT, text_color_disabled=DIM,
            command=lambda v: self._speed_var.set({"0.5×": 0.5, "1×": 1.0, "2×": 2.0}[v])
        )
        _speed_seg.set("1×")
        _speed_seg.pack()

        # ── GROUP ─────────────────────────────────────────────────────────
        _grp_frame = ctk.CTkFrame(bar, fg_color="transparent")
        _grp_frame.pack(side="left", padx=(0, 4), pady=8)
        ctk.CTkLabel(_grp_frame, text="GROUP",
                     font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        self._grouping_seg = ctk.CTkSegmentedButton(
            _grp_frame, values=["1","2","3","5","8"], width=120, height=22,
            font=ctk.CTkFont(size=9, weight="bold"),
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=BG4, unselected_hover_color=BG3,
            text_color=TEXT, text_color_disabled=DIM,
            command=lambda v: self._apply_ai_grouping(int(v), manual=True))
        self._grouping_seg.set("1")
        self._grouping_seg.pack()

        # ── SPACE ─────────────────────────────────────────────────────────
        _spc_frame = ctk.CTkFrame(bar, fg_color="transparent")
        _spc_frame.pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkLabel(_spc_frame, text="SPACE",
                     font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        self._spacing_seg = ctk.CTkSegmentedButton(
            _spc_frame, values=["0","2","4","8","16"], width=120, height=22,
            font=ctk.CTkFont(size=9, weight="bold"),
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=BG4, unselected_hover_color=BG3,
            text_color=TEXT, text_color_disabled=DIM,
            command=lambda v: self._apply_ai_spacing(int(v), manual=True))
        self._spacing_seg.set("0")
        self._spacing_seg.pack()

        # ── DIRECTION ─────────────────────────────────────────────────────────
        _dir_frame = ctk.CTkFrame(bar, fg_color="transparent")
        _dir_frame.pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkLabel(_dir_frame, text="DIRECTION",
                     font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        self._direction_seg = ctk.CTkSegmentedButton(
            _dir_frame, values=["FWD", "REV", "MIR"], width=100, height=22,
            font=ctk.CTkFont(size=9, weight="bold"),
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=BG4, unselected_hover_color=BG3,
            text_color=TEXT, text_color_disabled=DIM,
            command=lambda v: self._apply_ai_direction(
                {"FWD": "forward", "REV": "reverse", "MIR": "mirror"}[v]))
        self._direction_seg.set("FWD")
        self._direction_seg.pack()

        # ── OFFSET (read-only strip offsets display) ──────────────────────────
        _off_frame = ctk.CTkFrame(bar, fg_color="transparent")
        _off_frame.pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkLabel(_off_frame, text="OFFSET",
                     font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        self._offset_label = ctk.CTkLabel(
            _off_frame, text="0·0·0·0·0",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=ACCENT2, width=110, anchor="w")
        self._offset_label.pack()
        self._offset_label_last = ""

        # ── SPD-MUL (read-only per-strip speed display) ───────────────────────
        _spd_frame2 = ctk.CTkFrame(bar, fg_color="transparent")
        _spd_frame2.pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkLabel(_spd_frame2, text="SPD MUL",
                     font=("Helvetica Neue", 8), text_color=DIM).pack(anchor="w")
        self._spdmul_label = ctk.CTkLabel(
            _spd_frame2, text="1·1·1·1·1",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=ACCENT2, width=110, anchor="w")
        self._spdmul_label.pack()
        self._spdmul_label_last = ""

    # ── Canvas geometry ───────────────────────────────────────────────────────

    def _on_canvas_configure(self, event):
        self._rebuild_led_maps(event.width, event.height)

    def _rebuild_led_maps(self, w=None, h=None):
        if w is None:
            w = self._led_canvas.winfo_width()
            h = self._led_canvas.winfo_height()
        if w < 4 or h < 4:
            return
        devices = self._device_mgr.enabled_devices()
        if not devices:
            self._led_bands = []; self._canvas_w = w; self._canvas_h = h; return
        n   = len(devices)
        bh  = max(30, h // n)
        self._led_bands = []
        for i, dev in enumerate(devices):
            y0  = i * bh
            bhi = bh if i < n - 1 else max(30, h - y0)
            m   = Geometry.from_dict(dev.geometry).build_led_map(w, bhi)
            self._led_bands.append((dev, m, y0, bhi))
        self._canvas_w = w; self._canvas_h = h

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _refresh_devices(self):
        for w in self._device_frame.winfo_children(): w.destroy()
        for i, d in enumerate(self._device_mgr.devices):
            DeviceRow(self._device_frame, d, i, self._device_mgr,
                      self._refresh_devices, self._on_geometry_changed
                      ).pack(fill="x", pady=2)

    def _refresh_palettes(self):
        act      = self._palette_mgr._active
        palettes = self._palette_mgr.palettes
        rows     = [w for w in self._palette_frame.winfo_children()
                    if isinstance(w, PaletteRow)]
        # Fast path: same palette list — just update active highlight
        if len(rows) == len(palettes):
            for i, row in enumerate(rows):
                row.set_active(i == act)
            return
        # Slow path: palette list changed (add/delete) — full rebuild
        for w in self._palette_frame.winfo_children():
            w.destroy()
        for i, p in enumerate(palettes):
            PaletteRow(self._palette_frame, p, i,
                       self._select_palette, self._delete_palette,
                       active=(i == act)).pack(fill="x", pady=2)

    def _delete_palette(self, idx):
        self._palette_mgr.remove(idx)
        self._refresh_palettes()

    def _on_geometry_changed(self):
        self._refresh_devices(); self._rebuild_led_maps()

    def _switch_setup(self, value: str):
        """Called when the user picks a setup from the dropdown."""
        name = value.lower()
        self._device_mgr.switch_setup(name)
        # Propagate active setup to effects panel so Vault pool is gated correctly
        self._effects_panel._active_setup = name
        # Keep dropdown label in sync (it's already set by the OptionMenu)
        self._setup_menu.configure(
            values=[n.upper() for n in self._device_mgr.setup_names()])
        self._refresh_devices()
        self._rebuild_led_maps()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _select_effect(self, cls):
        # Choreo-mode buttons set the AnimationEngine mode on choreo_mgr and do NOT
        # create an _active_inst — the ChoreographyManager stays in control.
        choreo_mode = getattr(cls, "choreo_mode", None) if cls else None
        if choreo_mode is not None:
            self._active_inst = None
            self._active_choreo_mode_cls = cls
            if self._choreo_mgr is not None:
                self._choreo_mgr._mode = choreo_mode
            return
        # Selecting a normal effect clears any active choreo-mode override
        self._active_choreo_mode_cls = None
        if self._choreo_mgr is not None:
            pass  # choreo_mgr mode will be reset on next phrase change naturally

        self._active_inst = cls() if cls else None
        # Vault pattern button
        vp = getattr(cls, "vault_pattern", None) if cls else None
        self._vault_forced_pattern = vp
        if vp is not None:
            self._vault_gen_enabled = True
        # Advanced (DDPCanvas) pattern button
        ap = getattr(cls, "advanced_pattern", None) if cls else None
        self._adv_forced_pattern = ap
        if ap is not None:
            self._adv_gen_enabled = True
            if self._adv_renderer is not None:
                self._adv_renderer.reset()
        elif cls is not None and vp is None:
            # Switching away from advanced — reset renderer so canvas clears
            self._adv_gen_enabled = False
            if self._adv_renderer is not None:
                self._adv_renderer.reset()

    def _select_palette(self, idx):
        self._palette_mgr.set_active(idx)
        self._manual_palette = True   # lock out auto-swap until next phrase change
        self._refresh_palettes()

    def _toggle_randomize(self):
        self._randomizing = not self._randomizing
        if self._randomizing:
            self._rand_btn.configure(fg_color=ACCENT, text_color="#000000")
            self._swap_palette()          # immediate swap on enable
        else:
            self._rand_btn.configure(fg_color=BG4, text_color=ACCENT)

    def _swap_palette(self, phrase=None):
        """Swap to an appropriate palette. When phrase is given, selects from the
        phrase-appropriate pool. Falls back to random if no pool match.
        Safe to call from any thread via self.after(0, ...)."""
        palettes = self._palette_mgr.palettes
        n        = len(palettes)
        if n == 0:
            return
        cur     = self._palette_mgr._active
        chosen  = None

        if phrase and phrase in PHRASE_PALETTE_POOLS:
            pool_names = PHRASE_PALETTE_POOLS[phrase]
            # Prefer a different palette than the current one
            matches = [i for i, p in enumerate(palettes)
                       if p.name in pool_names and i != cur]
            if not matches:
                matches = [i for i, p in enumerate(palettes)
                           if p.name in pool_names]
            if matches:
                chosen = random.choice(matches)

        if chosen is None:
            # Fallback: any palette except current
            choices = [i for i in range(n) if i != cur]
            chosen  = random.choice(choices) if choices else cur

        old_name = self._palette_mgr.palettes[cur].name if cur < len(self._palette_mgr.palettes) else "?"
        self._palette_mgr.set_active(chosen)
        new_name = self._palette_mgr.palettes[chosen].name
        _log.info("PALETTE_SWAP  phrase=%r  %s → %s", phrase, old_name, new_name)
        self._tlog_write("PALETTE_SWAP", f"phrase={phrase}  {old_name} → {new_name}")
        self._refresh_palettes()

    def _toggle_kill(self):
        self._kill = not self._kill
        if self._kill:
            self._kill_btn.configure(text="▶  RESUME",
                                     fg_color="#1a3308", hover_color="#1a3308",
                                     text_color=ACCENT)
        else:
            self._kill_btn.configure(text="⬛  KILL ALL",
                                     fg_color=KILL_COL, hover_color="#991122",
                                     text_color="#fff")

    def _toggle_demo(self):
        self._demo_mode = not self._demo_mode
        if self._demo_mode:
            # Start a fresh demo player on every enable so it begins from intro
            self._demo_player = _DemoPlayer(bpm=125.0)
            # Load the demo plan into ChoreographyManager (track_id=1)
            if self._choreo_mgr is not None:
                self._choreo_mgr.apply_plan(1, self._demo_player.choreo_plan)
                _log.info("Demo mode ON: choreo plan loaded (%d phrases)",
                          len(self._demo_player.choreo_plan))
            self._demo_btn.configure(text="■  DEMO ON",
                                     fg_color=ACCENT2, hover_color="#005599",
                                     text_color="#fff")
            _log.info("Demo mode ON")
        else:
            self._demo_player = None
            self._demo_btn.configure(text="▶  DEMO",
                                     fg_color=BG3, hover_color=ACCENT2,
                                     text_color=DIM)
            _log.info("Demo mode OFF")

    def _add_device(self):
        def _do(name, ip, geom):
            led_count = geom.n_leds()
            dev = Device(name, ip, led_count, geometry=geom.to_dict())
            self._device_mgr.devices.append(dev)
            self._device_mgr.save()
            self._refresh_devices()
            self._rebuild_led_maps()
        AddDeviceDialog(self, _do)

    def _open_palette_editor(self):
        PaletteEditorDialog(self, self._palette_mgr, self._refresh_palettes)

    def _tap_bpm(self):
        now = time.perf_counter()
        self._tap_times.append(now)
        self._tap_times = [t for t in self._tap_times if now - t < 4.0]
        if len(self._tap_times) >= 2:
            intervals = [self._tap_times[k+1] - self._tap_times[k]
                         for k in range(len(self._tap_times) - 1)]
            bpm = round(60.0 / (sum(intervals) / len(intervals)), 1)
            self._bpm_var.set(bpm)
            self._bpm_str.set(str(bpm))

    def _get_last_px(self):
        with self._px_lock:
            return self._last_px.copy()

    # ── Keyboard shortcuts ────────────────────────────────────────────────────

    def _bind_keys(self):
        self.bind("<space>",  lambda e: self._toggle_kill())
        self.bind("<Escape>", lambda e: (setattr(self, "_kill", True) or
                                          self._kill_btn.configure(
                                              text="▶  RESUME", fg_color="#1a3308",
                                              hover_color="#1a3308", text_color=ACCENT)))
        self.bind("<t>", lambda e: self._tap_bpm())
        for k, i in {"i":0,"v":1,"c":2,"b":3,"u":4,"d":5,"o":6,"w":7,"a":8}.items():
            self.bind(f"<{k}>", lambda e, i=i: self._effects_panel.switch_cat(i))
        for n in range(1, 6):
            self.bind(str(n), lambda e, n=n: self._effects_panel.select_nth(n))

    # ── BLT client ────────────────────────────────────────────────────────────

    def _start_blt(self):
        try:
            _osc = int(_settings.get("network.osc_port", 9000))
            _udp = int(_settings.get("network.udp_port", 9001))
            self._blt = BLTClient(osc_port=_osc, udp_port=_udp,
                                  blt_base=_settings.get("network.blt_url",
                                                         "http://localhost:17081"))
            self._blt.on_phrase = self._on_blt_phrase
            _log.info("BLT client started — listening on OSC:%d UDP:%d", _osc, _udp)
        except Exception as e:
            _log.error("BLT start failed: %s", e)
            self._blt = None

    def _on_blt_phrase(self, player_num: int, phrase_kind: str):
        """Called from OSC thread immediately on phrase boundary — zero-latency reroll flag."""
        committed = self._blt_committed_player
        if committed and committed != player_num:
            return   # not the deck driving LEDs
        new_base = _phrase_base(phrase_kind)
        if new_base != self._blt_phrase_last:
            self._blt_reroll_flag = True
            self._blt_reroll_time = time.perf_counter()
            self._tlog_write("PHRASE_RECEIVED",
                f"player={player_num}  kind={phrase_kind}  →{new_base}")

    # ── Deck event tracker ────────────────────────────────────────────────────

    def _add_deck_event(self, player: int, event_type: str, detail: str = ""):
        """Record a deck-level event for the AI context window (max 20 entries)."""
        now = time.perf_counter()
        self._deck_events.append((now, player, event_type, detail))
        if len(self._deck_events) > 20:
            self._deck_events.pop(0)

    def _deck_events_summary(self, since: float = 15.0) -> str:
        """Human-readable recent events for the AI prompt (last `since` seconds)."""
        cutoff = time.perf_counter() - since
        recent = [(t, p, ev, d) for t, p, ev, d in self._deck_events if t >= cutoff]
        if not recent:
            return "none"
        lines = []
        for t, p, ev, d in recent:
            age = time.perf_counter() - t
            base = f"P{p} {ev} {age:.1f}s ago"
            lines.append(f"{base}  {d}".rstrip())
        return "  |  ".join(lines)

    def _other_deck_summary(self, committed_player: int) -> str:
        """One-line state of the non-committed deck(s) for AI context."""
        if not self._blt:
            return "unknown"
        parts = []
        for pn in range(1, 5):
            if pn == committed_player:
                continue
            s = self._blt.get_snapshot(pn)
            if not s or not (s.is_playing or s.has_track):
                continue
            phrase = _phrase_base(s.current_phrase.kind) if s.current_phrase else "?"
            parts.append(
                f"P{pn}: {'playing' if s.is_playing else 'stopped'}"
                f"  phrase={phrase}"
                f"  loop={'Y' if s.is_looping else 'N'}"
                f"  on_air={'Y' if s.on_air else 'N'}"
                f"  bpm={s.bpm:.0f}"
            )
        return "  |  ".join(parts) if parts else "none"

    def _best_blt_snap(self):
        if not self._blt:
            return None
        # Always follow committed deck — tick_selection() handles actual switching.
        # Do NOT fall back to another deck just because committed briefly pauses.
        committed = self._blt_committed_player
        if committed:
            snap = self._blt.get_snapshot(committed)
            if snap:
                return snap
        # No committed deck yet — fall back to any currently playing deck
        snaps = [self._blt.get_snapshot(n) for n in range(1, 5)]
        playing = [s for s in snaps if s and s.is_playing and s.bpm > 0]
        if not playing:
            return None
        return max(playing, key=lambda s: (s.on_air, s.is_master, s.bpm))

    def _best_blt_snap_for_ui(self):
        """Like _best_blt_snap but accepts any snap with a track — for status bar / AI tab."""
        if not self._blt:
            return None
        committed = self._blt_committed_player
        if committed:
            snap = self._blt.get_snapshot(committed)
            if snap and snap.has_track:
                return snap
        snaps = [self._blt.get_snapshot(n) for n in range(1, 5)]
        with_track = [s for s in snaps if s and s.has_track]
        if not with_track:
            return None
        playing = [s for s in with_track if s.is_playing]
        return max(playing or with_track, key=lambda s: (s.is_playing, s.on_air, s.is_master, s.bpm or 0))

    # ── ProDJ Link (waveforms only) ───────────────────────────────────────────

    def _start_prodj(self):
        try:
            self._prodj = _ProDj()
            self._prodj.set_client_change_callback(self._on_cdj_change)
            # Bind to 0.0.0.0 so all interfaces receive packets.
            # guess_own_iface() will auto-detect the correct ProDJ Link interface
            # from the CDJ keepalive IPs (typically link-local 169.254.x.x on en11).
            self._prodj.start()
            self._prodj.vcdj_enable()
            _log.info("ProDj started — waiting for auto-detect of DJ interface")
        except Exception as e:
            _log.error("ProDj start failed: %s", e)
            self._prodj = None

    def _on_cdj_change(self, player_number):
        if not self._prodj:
            return
        c = self._prodj.cl.getClient(player_number)
        if not c or not c.track_id or c.play_state == "no_track":
            return
        tid = c.track_id
        if self._cdj_last_tid.get(player_number) == tid:
            return
        self._cdj_last_tid[player_number] = tid
        pn   = c.loaded_player_number or player_number
        slot = c.loaded_slot or "usb"
        # BLT handles metadata/phrases/cues — only fetch waveform from prodj
        self._prodj.data.get_color_waveform(pn, slot, tid, lambda *a: None)
        self._prodj.data.get_beatgrid(pn, slot, tid, lambda *a: None)

        # Pre-fetch: notify AI tab as soon as track loads (even before playing).
        # Fires the Claude vision analysis early so it's cached by mix time.
        if hasattr(self, "_ai_control"):
            try:
                snap = self._best_blt_snap()
                wf_url = snap.waveform_url if snap and snap.track_id == tid else ""
                title  = getattr(snap, "title",       "") if snap else ""
                artist = getattr(snap, "artist",      "") if snap else ""
                dur_ms = getattr(snap, "duration_ms", 0)  if snap else 0
                self._ai_control.notify_track_loaded(wf_url, tid, title, artist, dur_ms)
            except Exception:
                pass

    def _best_cdj_client(self):
        """Legacy: used only for engine render. Prefer _best_blt_snap where possible."""
        snap = self._best_blt_snap()
        if snap:
            return _BLTEngineAdapter(snap)
        if not self._prodj:
            return None
        clients = self._prodj.cl.clients
        def _bpm_ok(c): return isinstance(c.bpm, (int, float)) and c.bpm > 0
        playing = [c for c in clients if c.play_state in ("playing", "cueing") and _bpm_ok(c)]
        if not playing:
            playing = [c for c in clients if _bpm_ok(c)]
        if not playing:
            return None
        return max(playing, key=lambda c: (getattr(c, "on_air", False), c.bpm or 0))

    # ── Render loop (background thread) ──────────────────────────────────────

    def _render_loop(self):
        interval   = 1.0 / self.FPS
        next_frame = time.perf_counter()
        _diag_frame = 0
        _log.info("RENDER_THREAD_START  fps=%d", self.FPS)
        self._tlog_write("RENDER_THREAD_START", f"fps={self.FPS}")
        while self._running:
            now = time.perf_counter()
            if now < next_frame:
                time.sleep(next_frame - now)
            next_frame += interval

            _diag_frame = (_diag_frame + 1) % 30   # log every ~0.5s at 60fps

            t          = time.perf_counter() - self._start_time
            brightness = self._bright_var.get()
            speed      = self._speed_var.get()

            # Use actual CDJ/BLT BPM and track position when available
            bpm = self._bpm_var.get()
            # Only trust BLT data when actual OSC beats have arrived recently.
            # The state file poller can populate stale session data (is_playing=True, bpm>0)
            # which would otherwise freeze t via dead-reckoning cap after ~14s.
            blt_beats_live = (BLT_OK and self._blt is not None
                              and self._blt._last_osc_beat > 0
                              and time.time() - self._blt._last_osc_beat < 5.0)
            if _diag_frame == 0:
                _beat_age = (time.time() - self._blt._last_osc_beat
                             if (self._blt and self._blt._last_osc_beat > 0) else -1)
                _snap_d = self._sb_snap_cache
                _eff_name = type(self._blt_phrase_inst).__name__ if self._blt_phrase_inst else "—"
                self._tlog_write("RENDER_DIAG",
                    f"beats_live={blt_beats_live}  beat_age={_beat_age:.2f}s  "
                    f"committed=P{self._blt_committed_player}  "
                    f"snap_tid={_snap_d.track_id if _snap_d else None}  "
                    f"playing={_snap_d.is_playing if _snap_d else None}  "
                    f"bpm={_snap_d.bpm if _snap_d else 0:.1f}  "
                    f"effect={_eff_name}")
            if PRODJ_OK or BLT_OK:
                best_for_bpm = self._best_cdj_client()
                if best_for_bpm:
                    raw = best_for_bpm.bpm
                    if isinstance(raw, (int, float)) and raw > 0:
                        pitch = getattr(best_for_bpm, "actual_pitch", 1.0) or 1.0
                        bpm = raw * pitch
                    # Use track position as t only when CDJ is live and playing
                    if (blt_beats_live
                            and getattr(best_for_bpm, 'is_playing', False)
                            and best_for_bpm.position is not None):
                        t = best_for_bpm.position

            devices = self._device_mgr.enabled_devices()
            if not devices:
                # No real devices — render to a virtual 1500-LED buffer so the
                # strip preview still animates.
                if self._active_inst:
                    # Manual effect takes priority
                    try:
                        virt_px = self._active_inst.render(
                            t, bpm, 1500,
                            self._palette_mgr.active.color, speed)
                        if brightness < 0.999:
                            virt_px = (virt_px.astype(np.float32) * brightness
                                       ).clip(0, 255).astype(np.uint8)
                        with self._px_lock:
                            self._last_px      = virt_px
                            self._last_px_devs = []
                    except Exception:
                        self._active_inst = None
                elif self._choreo_mgr is not None:
                    # No manual effect — show ChoreographyManager ambient preview.
                    # Demo mode: gebruik gesimuleerde snapshot. Anders: live BLT snap.
                    try:
                        if self._demo_mode and self._demo_player is not None:
                            _choreo_snap = self._demo_player.tick()
                            _choreo_t    = _choreo_snap.position_ms / 1000.0
                        else:
                            _choreo_snap = self._sb_snap_cache
                            _choreo_t    = t
                        _choreo_pal = self._palette_mgr.active.color
                        virt_px = self._choreo_mgr.render(
                            snap       = _choreo_snap,
                            t          = _choreo_t,
                            palette_fn = _choreo_pal,
                        )
                        # Demo mode: boost so dim phrases (intro 0.12, outro 0.10)
                        # are clearly visible in the preview canvas.
                        if self._demo_mode:
                            _mx = virt_px.max()
                            if _mx > 0:
                                _scale = min(3.5, 80.0 / float(_mx))
                                virt_px = np.clip(
                                    virt_px.astype(np.float32) * _scale,
                                    0, 255).astype(np.uint8)
                        # ── Back strip preview (no-devices / demo path) ───────
                        if self._cave_strip_renderer is not None:
                            try:
                                # Use plan's back_accent if available (tracks back_trigger too)
                                _cv_accent = getattr(self._choreo_mgr, "_current_back_accent", None)
                                if _cv_accent is None:
                                    _cv_phrase = (_choreo_snap.current_phrase.kind
                                                  if _choreo_snap and _choreo_snap.current_phrase
                                                  else "")
                                    _cv_phrase_base = _phrase_base(_cv_phrase) if _cv_phrase else ""
                                    _cv_accent = {"up": "pulse_build",
                                                  "chorus": "impact_flash"}.get(_cv_phrase_base, "off")
                                if _cv_accent != "off":
                                    _cv_bpm   = float(_choreo_snap.bpm or bpm or 125.0)
                                    _cv_bn    = getattr(_choreo_snap, "beat_number", 0) or 0
                                    # Demo mode: derive beat_age from position_ms so
                                    # CaveStripRenderer's new_beat gate (beat_age < 0.18s)
                                    # fires correctly without real OSC beat events.
                                    if self._demo_mode:
                                        _cv_beat_dur = 60.0 / max(_cv_bpm, 1.0)
                                        _cv_beat_frac = (_choreo_t / _cv_beat_dur) % 1.0
                                        _cv_bage = _cv_beat_frac * _cv_beat_dur
                                    else:
                                        _cv_bage = (time.perf_counter() - self._last_beat_t
                                                    if self._last_beat_t > 0 else 999.0)
                                    _cv_c1 = np.array(
                                        self._palette_mgr.active.color(0.0), dtype=np.float32)
                                    _cv_c2 = np.array(
                                        self._palette_mgr.active.color(0.65), dtype=np.float32)
                                    _cv_cs_phrase = {"pulse_build": "up",
                                                     "impact_flash": "chorus",
                                                     "strobe_burst": "strobe"}.get(_cv_accent, "down")
                                    _back_virt = self._cave_strip_renderer.render(
                                        t           = _choreo_t,
                                        bpm         = _cv_bpm,
                                        phrase      = _cv_cs_phrase,
                                        beat_n      = _cv_bn,
                                        beat_age    = _cv_bage,
                                        c1          = _cv_c1.astype(np.uint8),
                                        c2          = _cv_c2.astype(np.uint8),
                                    )
                                    if self._demo_mode:
                                        _bk_mx = _back_virt.max()
                                        if _bk_mx > 0:
                                            _back_virt = np.clip(
                                                _back_virt.astype(np.float32)
                                                * min(3.5, 80.0 / float(_bk_mx)),
                                                0, 255).astype(np.uint8)
                                    self._last_back_px = _back_virt
                                else:
                                    self._last_back_px = np.zeros((768, 3), dtype=np.uint8)
                            except Exception:
                                pass
                        # ─────────────────────────────────────────────────────
                        with self._px_lock:
                            self._last_px      = virt_px
                            self._last_px_devs = []
                    except Exception as _ce:
                        _log.debug("choreo preview fout: %s", _ce)
                        with self._px_lock:
                            self._last_px      = np.zeros((self.N_LEDS, 3), dtype=np.uint8)
                            self._last_px_devs = []
                else:
                    with self._px_lock:
                        self._last_px      = np.zeros((1, 3), dtype=np.uint8)
                        self._last_px_devs = []
                continue

            px_devs = []

            # Update snap cache for status bar regardless of beat liveness / active effect
            if BLT_OK and self._blt:
                _sb_snap = self._best_blt_snap()
                if _sb_snap is not None:
                    self._sb_snap_cache = _sb_snap

            # AI active: manual click overrides AI preview — do NOT clear _active_inst.
            # When the user clicks again to deselect, _active_inst becomes None and AI resumes.

            # Demo mode: render ChoreographyManager ONCE per frame (shared across devices)
            _demo_choreo_px = None   # cached 1500-LED result, reused by each non-back-strip device
            if self._demo_mode and self._demo_player is not None:
                _demo_snap = self._demo_player.tick()
                bpm = _demo_snap.bpm
                t   = _demo_snap.position_ms / 1000.0
                if self._choreo_mgr is not None:
                    try:
                        _demo_choreo_px = self._choreo_mgr.render(
                            snap       = _demo_snap,
                            t          = t,
                            palette_fn = self._palette_mgr.active.color,
                        )
                    except Exception as _dce:
                        _log.debug("demo choreo render: %s", _dce)
                    # Run CaveStripRenderer for back strip — update _last_back_px
                    if self._cave_strip_renderer is not None:
                        try:
                            # Plan's back_accent (updated by apply_plan_entry + back_trigger)
                            _cv_accent = getattr(self._choreo_mgr, "_current_back_accent", None)
                            if _cv_accent is None:
                                _cv_phrase = (_demo_snap.current_phrase.kind
                                              if _demo_snap and _demo_snap.current_phrase else "")
                                _cv_phrase_base = _phrase_base(_cv_phrase) if _cv_phrase else ""
                                _cv_accent = {"up": "pulse_build", "chorus": "impact_flash"}.get(
                                    _cv_phrase_base, "off")
                            _cv_bpm = float(_demo_snap.bpm or bpm or 125.0)
                            _cv_bn  = getattr(_demo_snap, "beat_number", 0) or 0
                            _cv_beat_dur  = 60.0 / max(_cv_bpm, 1.0)
                            _cv_beat_frac = (t / _cv_beat_dur) % 1.0
                            _cv_bage = _cv_beat_frac * _cv_beat_dur
                            if _cv_accent != "off":
                                _cv_c1 = np.array(self._palette_mgr.active.color(0.0), dtype=np.float32)
                                _cv_c2 = np.array(self._palette_mgr.active.color(0.65), dtype=np.float32)
                                _cv_cs_phrase = {"pulse_build": "up", "impact_flash": "chorus",
                                                  "strobe_burst": "strobe"}.get(_cv_accent, "down")
                                _back_demo = self._cave_strip_renderer.render(
                                    t=t, bpm=_cv_bpm, phrase=_cv_cs_phrase,
                                    beat_n=_cv_bn, beat_age=_cv_bage,
                                    c1=_cv_c1.astype(np.uint8), c2=_cv_c2.astype(np.uint8),
                                )
                                self._last_back_px = _back_demo
                            else:
                                self._last_back_px = np.zeros((768, 3), dtype=np.uint8)
                        except Exception:
                            pass
            else:
                _demo_snap = None

            phrase = ""   # ensure phrase is always defined for beat overlay
            for dev in devices:
                n = dev.led_count
                _dev_is_back = (
                    dev.geometry.get("type") == "linear"
                    and dev.led_count >= 500
                )
                if self._kill:
                    dev_px = np.zeros((n, 3), dtype=np.uint8)
                elif _demo_snap is not None and _demo_choreo_px is not None:
                    # Demo mode: use cached choreo render for main strips, back strip gets CaveStrip
                    if _dev_is_back:
                        dev_px = self._last_back_px.copy() if len(self._last_back_px) == n else \
                            np.zeros((n, 3), dtype=np.uint8)
                    else:
                        dev_px = _demo_choreo_px
                        if len(dev_px) != n:
                            dev_px = dev_px[:n] if len(dev_px) > n else np.pad(
                                dev_px, ((0, n - len(dev_px)), (0, 0)))
                    phrase = _demo_snap.current_phrase.kind if _demo_snap.current_phrase else ""
                elif _demo_snap is not None:
                    dev_px = np.zeros((n, 3), dtype=np.uint8)
                elif self._active_inst:
                    try:
                        dev_px = self._active_inst.render(
                            t, bpm, n,
                            self._palette_mgr.active.color, speed)
                    except Exception as e:
                        _log.warning("Manual effect render failed (%s): %s",
                                     type(self._active_inst).__name__, e)
                        self._active_inst = None
                        dev_px = np.zeros((n, 3), dtype=np.uint8)
                elif BLT_OK and self._blt and blt_beats_live:
                        snap = self._sb_snap_cache
                        if snap is None:
                            dev_px = np.zeros((n, 3), dtype=np.uint8)
                            continue
                        phrase    = _phrase_base(snap.current_phrase.kind) if snap.current_phrase else ""
                        beat_n    = snap.beat_number

                        # Track beat timers for beat overlay
                        _now_t = time.perf_counter()
                        if beat_n != self._last_beat_n:
                            self._last_beat_n = beat_n
                            self._last_beat_t = _now_t
                        if snap.beat_within_bar == 1 and beat_n != self._last_bar_beat_n:
                            self._last_bar_beat_n = beat_n
                            self._last_bar_t      = _now_t
                        if snap.beat_within_bar in (1, 3) and beat_n != self._last_2beat_beat_n:
                            self._last_2beat_beat_n = beat_n
                            self._last_2beat_t      = _now_t

                        player_changed = snap.player != self._blt_player_last

                        # ── Waveform analysis: track-change detection ────────────
                        _wv_tid = snap.track_id
                        _wv_pnum = snap.player
                        if _wv_tid and _wv_tid != self._ai_wv_tids.get(_wv_pnum):
                            self._ai_wv_tids[_wv_pnum] = _wv_tid
                            if self._ai_control:
                                self._ai_control.notify_track_loaded(
                                    snap.waveform_url, _wv_tid,
                                    getattr(snap, "title", ""), getattr(snap, "artist", ""),
                                    getattr(snap, "duration_ms", 0), player_num=_wv_pnum)

                        # Phrase index — look up against the phrases the plan was built with,
                        # not the live snapshot. ATF can re-send a longer phrase list after
                        # the plan is built, making live indices diverge from plan indices.
                        phrase_idx = 0
                        if (not snap.phrases and not snap.cues and snap.track_id
                                and self._ai_control):
                            # Waveform-only mode: AI synthesized phrases stored in _track_phrases.
                            # Determine current phrase by matching snap.beat_number against
                            # beat_start/beat_end ranges in the synthetic phrase list.
                            _synth_phrases = self._ai_control._track_phrases.get(snap.track_id)
                            if _synth_phrases:
                                _bn = snap.beat_number or 0
                                for _sp in _synth_phrases:
                                    if _sp["beat_start"] <= _bn < _sp["beat_end"]:
                                        phrase_idx = _sp["phrase_index"]
                                        phrase = _sp.get("kind", "verse")
                                        break
                                else:
                                    # Past all phrases (end of track) — use last
                                    phrase_idx = _synth_phrases[-1]["phrase_index"]
                                    phrase = _synth_phrases[-1].get("kind", "outro")
                        elif not snap.phrases and snap.cues and snap.track_id:
                            # No phrase data — advance through plan using cue time boundaries.
                            # Use position_ms (dead-reckoned, always accurate) vs time_ms.
                            # Fall back to beat-based sort when time_ms is 0.
                            pos_ms = snap.position_ms or 0
                            def _cue_ms(c):
                                if c.time_ms > 0:
                                    return c.time_ms
                                if c.beat > 0 and bpm > 0:
                                    return int(c.beat / bpm * 60000)
                                return 0
                            _sorted_cues = sorted(snap.cues, key=_cue_ms)
                            for _ci, _c in enumerate(_sorted_cues):
                                _c_ms   = _cue_ms(_c)
                                _next_ms = (_cue_ms(_sorted_cues[_ci + 1])
                                            if _ci + 1 < len(_sorted_cues) else 999999999)
                                if _c_ms <= pos_ms < _next_ms:
                                    phrase_idx = _ci
                                    break
                            # Synthesise a phrase label from the cue so phrase_changed fires
                            if _sorted_cues and phrase_idx < len(_sorted_cues):
                                _cc = _sorted_cues[phrase_idx]
                                _nm = (_cc.name or "").lower()
                                if any(w in _nm for w in ("drop", "chorus", "peak")):
                                    phrase = "chorus"
                                elif any(w in _nm for w in ("build", "up", "rise")):
                                    phrase = "up"
                                elif any(w in _nm for w in ("break", "down", "low")):
                                    phrase = "down"
                                elif any(w in _nm for w in ("intro",)):
                                    phrase = "intro"
                                elif any(w in _nm for w in ("outro",)):
                                    phrase = "outro"
                                else:
                                    phrase = f"cue_{phrase_idx}"
                        if snap.current_phrase and snap.phrases:
                            # Prefer to look up phrase_idx directly from the AI plan's
                            # beat_start/beat_end ranges using snap.beat_number. This works
                            # whether the plan was built from real or synthesized phrases —
                            # no dependency on plan-phrase beat_start matching real ones.
                            _resolved = False
                            if self._ai_control:
                                _live_plan = self._ai_control._track_plans.get(snap.track_id)
                                if _live_plan:
                                    _bn = snap.beat_number or 0
                                    _last_key = max(_live_plan.keys())
                                    phrase_idx = _last_key
                                    for _pi in sorted(_live_plan.keys()):
                                        _pe = _live_plan[_pi]
                                        _pe_bs = _pe.get("beat_start") or 0
                                        _pe_be = _pe.get("beat_end") or 0
                                        if _pe_bs > 0 and _pe_be > _pe_bs:
                                            if _pe_bs <= _bn < _pe_be:
                                                phrase_idx = _pi
                                                _resolved = True
                                                break
                                    else:
                                        _resolved = True  # defaulted to last entry
                            if not _resolved:
                                # No plan yet or plan has no beat ranges — match by real phrase list
                                _cur_bs = snap.current_phrase.beat_start
                                for _pi, _pp in enumerate(snap.phrases):
                                    if _pp.beat_start == _cur_bs:
                                        phrase_idx = _pi
                                        break
                                else:
                                    phrase_idx = min(range(len(snap.phrases)),
                                                     key=lambda i: abs(snap.phrases[i].beat_start - _cur_bs),
                                                     default=0)

                        # Player switch alone does NOT reset the effect — only an actual
                        # phrase-kind or phrase-index change does.
                        phrase_changed = (phrase != self._blt_phrase_last
                                          or phrase_idx != self._blt_phrase_idx_last)
                        self._blt_reroll_flag = False
                        # Release manual overrides on phrase change so AI resumes control
                        if phrase_changed:
                            self._manual_grouping = False
                            self._manual_spacing  = False
                            self._manual_palette  = False
                            # Choreo_mgr resets its own flags in _apply_plan_overrides;
                            # also clear here in case choreo_mgr is not driving this path
                            if self._choreo_mgr is not None:
                                self._choreo_mgr._manual_grouping = False
                                self._choreo_mgr._manual_spacing  = False
                            self._blt_phrase_change_bn = beat_n  # mark for cue_press suppression

                        # Cue press: beat jumped non-sequentially (>4 beats, not just +1).
                        # Suppressed for 4 beats after a phrase change — prevents double-trigger
                        # when a cue lands on a phrase boundary (beat OSC fires before phrase OSC).
                        if player_changed:
                            self._blt_last_beat_n = 0
                            self._add_deck_event(snap.player, "deck_switched",
                                                 f"from=P{self._blt_player_last}")
                        # ── AI context derivation ──────────────────────────────────
                        loop_active           = snap.is_looping

                        last_bn   = self._blt_last_beat_n
                        _near_phrase_change = abs(beat_n - self._blt_phrase_change_bn) < 4
                        cue_press = (not phrase_changed and not _near_phrase_change
                                     and not loop_active
                                     and last_bn > 0
                                     and beat_n != last_bn + 1
                                     and abs(beat_n - last_bn) > 4)
                        if cue_press:
                            self._add_deck_event(snap.player, "cue_press",
                                                 f"beat_jump={abs(beat_n - last_bn)}")
                        self._blt_last_beat_n = beat_n

                        # 4-bar reroll while in same phrase
                        reroll_due = (beat_n > 0
                                      and beat_n - self._blt_effect_beat >= 16)  # 4 bars
                        beats_to_next         = snap.beats_to_next_phrase
                        if snap.current_phrase:
                            beat_in_phrase = beat_n - snap.current_phrase.beat_start
                        elif snap.cues and not snap.phrases:
                            _sc2 = sorted(snap.cues, key=_cue_ms)
                            _cue_start_ms = _cue_ms(_sc2[phrase_idx]) if phrase_idx < len(_sc2) else 0
                            beat_in_phrase = int((pos_ms - _cue_start_ms) / 60000 * bpm) if bpm > 0 else 0
                        else:
                            beat_in_phrase = 0
                        total_beats_in_phrase = (snap.current_phrase.beat_end
                                                 - snap.current_phrase.beat_start
                                                 if snap.current_phrase else 32)
                        next_phrase = None
                        if snap.current_phrase and snap.phrases:
                            for _p in snap.phrases:
                                if _p.beat_start > snap.current_phrase.beat_start:
                                    next_phrase = _phrase_base(_p.kind)
                                    break
                        ai_active = self._ai_active_flag

                        # Log beats: up/chorus every bar; other phrases every 8 beats
                        if beat_n != last_bn:
                            _log_eff  = type(self._blt_phrase_inst).__name__ if self._blt_phrase_inst else "—"
                            if self._ai_control and snap.track_id:
                                self._ai_control.record_beat(
                                    snap.track_id, beat_n, phrase, _log_eff,
                                    self._bright_var.get(), self._speed_var.get(),
                                    beat_in_phrase)
                            _should_log = False
                            if phrase in ("up", "chorus"):
                                # Log every bar (beat 1 of each 4-beat bar)
                                _should_log = (snap.beat_within_bar == 1)
                            else:
                                # Every 8 beats for other phrases
                                _should_log = (beat_in_phrase % 8 == 0)
                            if _should_log:
                                _log_br = self._bright_var.get()
                                _log_sp = self._speed_var.get()
                                self._tlog_write("BEAT",
                                    f"P{snap.player}  bn={beat_n}  bar={snap.beat_within_bar}  "
                                    f"phrase={phrase}  bip={beat_in_phrase}  "
                                    f"total={total_beats_in_phrase}  next={next_phrase or '—'}  "
                                    f"bpm={bpm:.1f}  effect={_log_eff}  "
                                    f"bright={_log_br:.2f}  speed={_log_sp:.2f}  "
                                    f"ai={'ON' if ai_active else 'off'}  "
                                    f"armed={self._chorus_impact_pre_armed}  "
                                    f"impact={'YES' if type(self._blt_phrase_inst).__name__ == 'ChorusImpactFlash' else 'no'}")

                        # ── Pre-arm chorus impact on last beat of "up" ────────────────
                        # Schedule the strobe one beat early so it fires at the actual
                        # downbeat, not when BLT's OSC phrase message eventually arrives.
                        if (phrase == "up"
                                and next_phrase == "chorus"
                                and bpm > 0
                                and total_beats_in_phrase > 0
                                and beat_in_phrase == total_beats_in_phrase - 1
                                and not self._chorus_impact_pre_armed
                                and type(self._blt_phrase_inst).__name__ != "ChorusImpactFlash"):
                            beat_dur = 60.0 / bpm
                            self._chorus_impact_fire_t    = time.perf_counter() + beat_dur
                            self._chorus_impact_pre_armed = True
                            self._tlog_write("CHORUS_IMPACT_ARMED",
                                f"fires_in={beat_dur*1000:.0f}ms  beat={beat_in_phrase}/{total_beats_in_phrase}")

                        # Reset arm flag when leaving "up" phrase
                        if phrase != "up":
                            self._chorus_impact_pre_armed = False
                            if (self._chorus_impact_fire_t is not None
                                    and type(self._blt_phrase_inst).__name__ != "ChorusImpactFlash"):
                                self._chorus_impact_fire_t = None  # cancelled (phrase changed differently)

                        # Fire pre-armed impact
                        if (self._chorus_impact_fire_t is not None
                                and time.perf_counter() >= self._chorus_impact_fire_t
                                and type(self._blt_phrase_inst).__name__ != "ChorusImpactFlash"):
                            self._chorus_impact_fire_t = None
                            self._chorus_impact_pending_inst = None  # OSC will fill this in
                            self._blt_phrase_inst    = ChorusImpactFlash()
                            self._effect_start_time  = time.perf_counter()
                            self._chorus_impact_active = True
                            self._tlog_write("CHORUS_IMPACT_PRE_FIRED",
                                f"phrase={phrase}  beat={beat_in_phrase}/{total_beats_in_phrase}")

                        # Plan catch-up: plan arrived after fallback fired for current phrase
                        plan_catch_up = (
                            ai_active
                            and not phrase_changed and not cue_press
                            and snap.track_id
                            and (snap.track_id, phrase_idx) not in self._ai_plan_applied
                            and self._ai_control.get_plan_for_phrase(
                                snap.track_id, phrase_idx) is not None
                        )

                        # ── AI: loop state tracking ────────────────────────────────
                        loop_just_activated   = loop_active and not self._last_loop_state
                        loop_just_deactivated = not loop_active and self._last_loop_state

                        if loop_just_activated:
                            self._loop_length        = None
                            self._loop_decision_beat = beat_in_phrase
                        if loop_just_deactivated:
                            self._loop_length        = None
                            self._loop_decision_beat = None
                        self._last_loop_state = loop_active

                        # Detect loop length from beat position cycling back
                        if (loop_active and self._loop_length is None
                                and self._loop_decision_beat is not None
                                and beat_in_phrase < self._loop_decision_beat):
                            self._loop_length = self._loop_decision_beat - beat_in_phrase

                        # ── AI: request track plan for any deck with unplanned track ──
                        # Plans are built in background even when AI is OFF so they're
                        # ready the moment the user enables AI. Only application of
                        # the plan is gated on ai_active.
                        if self._ai_control and self._blt:
                            _committed_p = snap.player
                            _plan_order = ([_committed_p] +
                                           [p for p in range(1, 4) if p != _committed_p])
                            for _p_num in _plan_order:
                                _p_snap = (snap if snap.player == _p_num
                                           else self._blt.get_snapshot(_p_num))
                                _has_analysis = bool(
                                    self._ai_control._track_analysis_cache.get(_p_snap.track_id))
                                # Start planning as soon as track_id + title + BPM are known.
                                # If no phrase data yet, plan with empty phrases (synthetic plan)
                                # and re-plan when ATF phrases arrive (synthetic eviction handles it).
                                if not (_p_snap.has_track
                                        and (_p_snap.bpm > 0 or _p_snap.track_bpm > 0)
                                        and _p_snap.title):
                                    continue
                                _p_tid = _p_snap.track_id
                                if _p_tid in self._ai_control._planning_tids:
                                    continue  # already in-flight for this track
                                # Committed deck can start even if non-committed plan running.
                                # Non-committed decks blocked while anything is in-flight.
                                if self._ai_control._planning_tids and _p_num != _committed_p:
                                    break
                                if _p_tid in self._ai_control._track_plans:
                                    # If the existing plan was built with 0 BLT phrases
                                    # (synthetic/waveform-only) but real phrases are now
                                    # available → evict and re-plan with real phrase data.
                                    _was_synth = (
                                        self._ai_control._planning_n_phrases.get(_p_tid, -1) == 0
                                        and bool(_p_snap.phrases))
                                    if _was_synth:
                                        # Re-plan with real phrase data.
                                        # Keep synthetic plan in _track_plans as fallback —
                                        # new plan overwrites it when API responds.
                                        self._ai_control._planning_tids.discard(_p_tid)
                                        self._ai_plan_applied = {
                                            k for k in self._ai_plan_applied if k[0] != _p_tid}
                                        # Clear the "already requested" guard so plan_track fires
                                        self._ai_plan_tids.pop(_p_num, None)
                                        _log.info("Synthetic plan kept as fallback for track %s — "
                                                  "re-planning with %d real phrases",
                                                  _p_tid, len(_p_snap.phrases))
                                        # Fall through to plan_track call below
                                    else:
                                        # Plan already exists for this track (maybe built by another deck)
                                        self._ai_plan_tids[_p_num] = _p_tid  # mark covered
                                        continue
                                if self._ai_plan_tids.get(_p_num) == _p_tid:
                                    continue  # already requested by this deck (in-flight or failed)
                                _fail_t = self._ai_control._plan_fail_times.get(_p_tid, 0)
                                if time.time() - _fail_t < 15.0:
                                    continue  # failed recently — wait 15s before retry
                                self._ai_plan_tids[_p_num] = _p_tid
                                # Clear render guard for this deck so new track gets fresh table
                                self._ai_control._deck_rendered.discard((_p_num, _p_tid))
                                _plan_phrases = [
                                    {"phrase_index": _pi,
                                     "kind": _phrase_base(_pp.kind),
                                     "beat_start": _pp.beat_start,
                                     "beat_end": _pp.beat_end,
                                     "duration_beats": max(1, _pp.beat_end - _pp.beat_start)}
                                    for _pi, _pp in enumerate(_p_snap.phrases)
                                ]
                                _plan_title  = getattr(_p_snap, "title", "")
                                _plan_artist = getattr(_p_snap, "artist", "")
                                _plan_bpm    = _p_snap.track_bpm or _p_snap.bpm or bpm
                                _plan_dur    = getattr(_p_snap, "duration_ms", 0)
                                _plan_pnum   = _p_num
                                _plan_cues   = [
                                    {"label": c.label, "name": c.name, "type": c.type,
                                     "time_ms": c.time_ms, "beat": c.beat,
                                     **({"loop_end_ms": c.loop_end_ms} if c.loop_end_ms else {})}
                                    for c in (_p_snap.cues or [])
                                ]
                                threading.Thread(
                                    target=lambda tid=_p_tid, ph=_plan_phrases,
                                                  t=_plan_title, a=_plan_artist,
                                                  bm=_plan_bpm, dm=_plan_dur, pn=_plan_pnum,
                                                  cu=_plan_cues:
                                        self._ai_control.plan_track(tid, ph, t, a, bm, dm,
                                                                     player_num=pn, cues=cu),
                                    daemon=True,
                                ).start()
                                self._tlog_write("TRACK_PLAN_REQUESTED",
                                    f"deck=P{_p_num}  track_id={_p_tid}  "
                                    f"title={_plan_title!r}  n_phrases={len(_plan_phrases)}  "
                                    f"bpm={_plan_bpm:.1f}")
                                break  # one plan at a time — revisit other decks next iteration

                        # ── AI: mid-phrase changes from plan ──────────────────
                        _light_phrase = phrase in ("intro", "verse", "down", "outro")
                        # Allow mid-phrase changes for synth-phrase and cue-only tracks too —
                        # beat_in_phrase is valid for all track modes (snap.phrases is the guard
                        # for OSC phrase tracks, but we also have synth phrases and cue mode).
                        _has_plan_phrases = bool(
                            snap.phrases
                            or (self._ai_control and
                                self._ai_control._track_phrases.get(snap.track_id))
                            or snap.cues
                        )
                        if (ai_active and not loop_active and not phrase_changed
                                and snap.track_id and _has_plan_phrases):
                            _mp_key = (snap.track_id, phrase_idx, beat_in_phrase)
                            if _mp_key not in self._mid_phrase_applied:
                                _mp_change = self._ai_control.check_mid_phrase(
                                    snap.track_id, phrase_idx, beat_in_phrase)
                                if _mp_change:
                                    self._mid_phrase_applied.add(_mp_key)
                                    _mp_effect = _mp_change.get("effect")
                                    if _mp_effect:
                                        self._blt_phrase_inst = self._pick_effect(
                                            phrase, _mp_effect)
                                        self._effect_start_time = time.perf_counter()
                                        self._current_effect_name = type(
                                            self._blt_phrase_inst).__name__
                                    if _mp_change.get("palette"):
                                        self._apply_ai_palette(_mp_change["palette"], phrase)
                                    if "direction" in _mp_change:
                                        self._apply_ai_direction(_mp_change["direction"])
                                    if "grouping" in _mp_change:
                                        self._apply_ai_grouping(1 if _light_phrase else _mp_change["grouping"])
                                    if "spacing" in _mp_change:
                                        self._apply_ai_spacing(0 if _light_phrase else _mp_change["spacing"])
                                    if "density" in _mp_change:
                                        self._apply_ai_density(_mp_change["density"])
                                    if "active_strips" in _mp_change:
                                        self._apply_ai_active_strips(_mp_change["active_strips"])
                                    # Clamp mid-phrase brightness/speed to per-phrase ceilings
                                    _MP_CEIL = {
                                        "down":  (0.60, 0.75),
                                        "intro": (0.45, 0.70),
                                        "outro": (0.40, 0.65),
                                    }
                                    _mp_bc, _mp_sc = _MP_CEIL.get(phrase, (1.0, 2.2))
                                    _mp_br = min(_mp_bc, _mp_change["brightness"]) if "brightness" in _mp_change else None
                                    _mp_sp = min(_mp_sc, _mp_change["speed"])       if "speed"      in _mp_change else None
                                    _mp_ov = _mp_change.get("beat_overlay")
                                    # down/intro/outro overlay forced off
                                    if phrase in ("down", "intro", "outro"):
                                        _mp_ov = "off"
                                    self.after(0, lambda br=_mp_br, sp=_mp_sp, ov=_mp_ov: (
                                        self._apply_ai_brightness(br) if br is not None else None,
                                        self._apply_ai_speed(sp)       if sp is not None else None,
                                        self._apply_ai_beat_overlay(ov) if ov is not None else None,
                                    ))
                                    self._tlog_write("MIDPHRASE_FROM_PLAN",
                                        f"track={snap.track_id}  phrase_idx={phrase_idx}  "
                                        f"at_beat={beat_in_phrase}  "
                                        f"effect={self._current_effect_name}  "
                                        f"bright={_mp_change.get('brightness','—')}")

                        # ── Continuous speed/brightness ramp for "up" and "chorus" ──
                        # "up": ramp aggressively — energy must visibly mount
                        # "chorus": slow creep so sustained energy doesn't feel static
                        if (ai_active and phrase in ("up", "chorus") and not phrase_changed
                                and beat_n != last_bn and total_beats_in_phrase > 0):
                            _t = min(1.0, beat_in_phrase / max(1.0, total_beats_in_phrase - 1))
                            _last4 = (next_phrase == "chorus"
                                      and total_beats_in_phrase > 0
                                      and beat_in_phrase >= total_beats_in_phrase - 4)
                            if phrase == "up":
                                _base_sp = getattr(self, "_up_ramp_base_speed", 0.9)
                                _base_br = getattr(self, "_up_ramp_base_bright", 0.60)
                                if _last4:
                                    # Final slingshot: slam to max brightness/speed before the drop
                                    _tgt_sp = 2.5
                                    _tgt_br = 1.0
                                else:
                                    _tgt_sp = min(2.2, _base_sp * 1.9)
                                    _tgt_br = min(1.0, _base_br + 0.38)  # peak up to full brightness
                            else:  # chorus — subtle escalation, never decrease
                                _base_sp = getattr(self, "_chorus_ramp_base_speed",
                                                   self._speed_var.get())
                                _tgt_sp  = min(2.2, _base_sp * 1.15)   # +15% over chorus
                                _base_br = getattr(self, "_chorus_ramp_base_bright",
                                                   self._bright_var.get())
                                _tgt_br  = min(1.0, _base_br + 0.10)   # +0.10 over chorus
                            _ramp_sp = _base_sp + _t * (_tgt_sp - _base_sp)
                            _ramp_br = _base_br + _t * (_tgt_br - _base_br)
                            _cur_sp  = self._speed_var.get()
                            _cur_br  = self._bright_var.get()
                            # Only push up, never pull down (mid_phrase_changes can override higher)
                            if _ramp_sp > _cur_sp + 0.05:
                                self.after(0, lambda s=_ramp_sp: self._apply_ai_speed(s))
                            if _ramp_br > _cur_br + 0.02:
                                self.after(0, lambda b=_ramp_br: self._apply_ai_brightness(b))
                            # Overlay escalation: ramp speed drives visual intensity
                            # bar → 2beat → beat as speed climbs (up phrase only)
                            if phrase == "up":
                                _ov_now = self._ai_beat_overlay
                                if _last4:
                                    # Last 4 beats before the drop: force beat overlay always
                                    if _ov_now != "beat":
                                        self.after(0, lambda: self._apply_ai_beat_overlay("beat"))
                                elif _ramp_sp >= 1.3 and _ov_now == "bar":
                                    self.after(0, lambda: self._apply_ai_beat_overlay("2beat"))
                                elif _ramp_sp >= 1.6 and _ov_now == "2beat":
                                    self.after(0, lambda: self._apply_ai_beat_overlay("beat"))

                        # ── Waveform energy modulation (beat-rate, within-phrase) ──
                        # Bass → speed nudge (kick energy = faster feel)
                        # Treble → brightness nudge (shimmer = brighter)
                        # Applied every beat as a small push on top of AI plan values.
                        # Never overrides floors/ceilings — _apply_ai_* clamps.
                        if ai_active and beat_n != last_bn:
                            _bass = snap.bass_energy    # 0-1
                            _treb = snap.treble_energy  # 0-1
                            # Only nudge if energy is meaningfully non-zero
                            if _bass > 0.05 or _treb > 0.05:
                                _sp_now = self._speed_var.get()
                                _br_now = self._bright_var.get()
                                # Bass drives speed: +0→+0.08 over 0-1 bass range
                                _sp_energy = _sp_now + _bass * 0.08
                                # Treble drives brightness: +0→+0.03
                                _br_energy = _br_now + _treb * 0.03
                                # Only push up, never pull down (let ramp/phrases govern floor)
                                if _sp_energy > _sp_now + 0.04:
                                    self.after(0, lambda s=_sp_energy: self._apply_ai_speed(s))
                                if _br_energy > _br_now + 0.02:
                                    self.after(0, lambda b=_br_energy: self._apply_ai_brightness(b))

                        # ── Effect / palette change ────────────────────────────────
                        if player_changed and not phrase_changed:
                            # Deck switched mid-phrase — log it but keep running effect
                            self._blt_player_last = snap.player
                            self._tlog_write("PLAYER_SWITCH_SEAMLESS",
                                f"P{self._blt_player_last}→P{snap.player}  "
                                f"phrase={phrase}  effect_continues=YES")

                        if phrase_changed or cue_press or plan_catch_up or (reroll_due and not ai_active):
                            self._active_inst     = None
                            old_phrase            = self._blt_phrase_last
                            self._blt_phrase_last = phrase
                            self._blt_phrase_idx_last = phrase_idx
                            self._blt_player_last = snap.player
                            self._blt_effect_beat = beat_n
                            _change_t             = time.perf_counter()

                            if phrase_changed:
                                self._add_deck_event(snap.player, "phrase_change",
                                                     f"{old_phrase or '—'}→{phrase}")
                                # For same-kind / idx-only transitions, _on_blt_phrase didn't
                                # fire → _blt_reroll_time is stale from the previous kind change.
                                # Reset it to detect-time so osc_lag and osc_to_effect are correct.
                                if phrase == old_phrase:
                                    self._blt_reroll_time = _change_t
                                osc_lag_ms = ((_change_t - self._blt_reroll_time) * 1000
                                              if self._blt_reroll_time > 0 else -1)
                                self._phrase_detect_t = _change_t
                                self._tlog_write("PHRASE_DETECTED",
                                    f"{old_phrase or '—'}→{phrase}  beat={beat_n}  "
                                    f"bpm={bpm:.1f}  P{snap.player}  "
                                    f"osc_lag={osc_lag_ms:.1f}ms  "
                                    f"has_plan={'YES' if snap.track_id in self._ai_control._track_plans else 'NO'}")

                            if (phrase_changed or plan_catch_up) and ai_active:
                                # Clear mid-phrase tracking only on true phrase change
                                if phrase_changed:
                                    self._mid_phrase_applied.clear()
                                plan_entry = self._ai_control.get_plan_for_phrase(
                                    snap.track_id, phrase_idx)
                                if plan_entry:
                                    # BPM pitch scaling: plan was made at track_bpm;
                                    # scale speed proportionally to live pitch.
                                    _bpm_scale = (snap.bpm / snap.track_bpm
                                                  if snap.track_bpm > 0 and snap.bpm > 0
                                                  else 1.0)
                                    _plan_speed = plan_entry.get("speed")
                                    _eff_speed  = ((_plan_speed * _bpm_scale)
                                                   if _plan_speed is not None else None)
                                    # Clamp brightness/speed to phrase floors AND ceilings.
                                    # (br_floor, sp_floor, br_ceil, sp_ceil)  None = no limit
                                    _LIVE_LIMITS = {
                                        # (br_floor, sp_floor, br_ceil, sp_ceil)
                                        "chorus": (0.88, 1.4,  1.0,  2.5),
                                        "up":     (0.65, 1.0,  1.0,  2.5),
                                        "verse":  (0.50, 0.6,  0.80, 2.2),
                                        "bridge": (0.45, 0.5,  0.75, 2.2),
                                        "down":   (0.50, None, 0.62, 0.80),
                                        "intro":  (None, None, 0.50, 0.75),
                                        "outro":  (0.20, None, 0.42, 0.65),
                                    }
                                    _ll = _LIVE_LIMITS.get(phrase)
                                    if _ll:
                                        br_fl, sp_fl, br_cl, sp_cl = _ll
                                        _br = plan_entry.get("brightness")
                                        if _br is not None:
                                            _br = max(_br, br_fl) if br_fl is not None else _br
                                            _br = min(_br, br_cl) if br_cl is not None else _br
                                            if _br != plan_entry.get("brightness"):
                                                plan_entry = dict(plan_entry)
                                                plan_entry["brightness"] = _br
                                        if _eff_speed is not None:
                                            if sp_fl is not None and _eff_speed < sp_fl:
                                                _eff_speed = sp_fl
                                            if sp_cl is not None and _eff_speed > sp_cl:
                                                _eff_speed = sp_cl
                                    # Enforce down/intro/outro overlay = "off" regardless of plan
                                    if phrase in ("down", "intro", "outro"):
                                        plan_entry = dict(plan_entry)
                                        plan_entry["beat_overlay"] = "off"
                                    _prev_cls = type(self._blt_phrase_inst) if self._blt_phrase_inst else None
                                    self._blt_phrase_inst = self._pick_effect(
                                        phrase, plan_entry.get("effect"))
                                    # Hard enforcement: never same effect class at phrase boundary.
                                    # If pick returned the same class, force a random alternative.
                                    if (phrase_changed
                                            and type(self._blt_phrase_inst) is _prev_cls
                                            and _prev_cls is not None):
                                        self._blt_phrase_inst = self._pick_effect(phrase, None)
                                    self._effect_start_time   = time.perf_counter()
                                    self._current_effect_name = type(
                                        self._blt_phrase_inst).__name__
                                    self._apply_ai_palette(plan_entry.get("palette"), phrase)
                                    self._apply_ai_direction(
                                        plan_entry.get("direction") or "forward")
                                    self._apply_ai_grouping(1 if _light_phrase else (plan_entry.get("grouping") or 1))
                                    self._apply_ai_spacing(0 if _light_phrase else (plan_entry.get("spacing") or 0))
                                    self._apply_ai_offset(plan_entry.get("offset") or 0.0)
                                    self._apply_ai_density(plan_entry.get("density") or 1.0)
                                    self._apply_ai_active_strips(
                                        plan_entry.get("active_strips") or [1, 2, 3, 4, 5])
                                    self.after(0, lambda e=plan_entry, es=_eff_speed: (
                                        self._apply_ai_brightness(e.get("brightness")),
                                        self._apply_ai_speed(es),
                                        self._apply_ai_beat_overlay(
                                            e.get("beat_overlay") or "bar"),
                                        self._apply_ai_render_mode(
                                            e.get("render_mode") or "full"),
                                    ))
                                    # Store ramp bases for continuous speed/brightness ramps
                                    if phrase == "up":
                                        self._up_ramp_base_speed   = _eff_speed or (plan_entry.get("speed") or 0.9)
                                        self._up_ramp_base_bright  = plan_entry.get("brightness") or 0.60
                                    elif phrase == "chorus":
                                        self._chorus_ramp_base_speed  = _eff_speed or (plan_entry.get("speed") or 1.2)
                                        self._chorus_ramp_base_bright = plan_entry.get("brightness") or 0.75
                                    _pal_name = self._palette_mgr.palettes[
                                        self._palette_mgr._active].name
                                    self._ddp_log_pending = True
                                    self._record_phrase_history(
                                        phrase, self._current_effect_name, _pal_name)
                                    _osc_to_effect = ((time.perf_counter() - self._blt_reroll_time) * 1000
                                                      if self._blt_reroll_time > 0 else -1)
                                    # Only record latency for real-time applies, not plan catch-up
                                    # (catch-up applies happen when plan arrives after the phrase
                                    # already fired — osc_to_effect includes API build time ~40s)
                                    if self._ai_control and _osc_to_effect > 0 and not plan_catch_up:
                                        self._ai_control.record_effect_applied(
                                            snap.track_id, phrase, _osc_to_effect,
                                            self._current_effect_name)
                                    self._ai_plan_applied.add((snap.track_id, phrase_idx))
                                    self._tlog_write("EFFECT_FROM_PLAN",
                                        f"deck=P{snap.player}  phrase={phrase}  "
                                        f"effect={self._current_effect_name}  "
                                        f"palette={_pal_name}  "
                                        f"bright={plan_entry.get('brightness','?')}  "
                                        f"speed={plan_entry.get('speed','?')}  "
                                        f"bpm_scale={_bpm_scale:.3f}  "
                                        f"dir={plan_entry.get('direction','forward')}  "
                                        f"group={plan_entry.get('grouping',1)}  "
                                        f"spacing={plan_entry.get('spacing',0)}  "
                                        f"phrase_idx={phrase_idx}  "
                                        f"catchup={'YES' if plan_catch_up else 'NO'}  "
                                        f"osc_to_effect={_osc_to_effect:.1f}ms")
                                    self.after(0, lambda tid=snap.track_id, pidx=phrase_idx,
                                                       pk=phrase, pn=snap.player:
                                               self._ai_control.set_current_phrase(tid, pidx, pk,
                                                                                    player_num=pn))

                                    # Catchup fast-forward: apply cumulative state of any
                                    # mid-phrase changes that already passed before plan arrived.
                                    # Without this, those beats are silently skipped and the plan
                                    # plays from the base entry state regardless of beat position.
                                    if plan_catch_up and beat_in_phrase > 0 and plan_entry:
                                        _past = sorted(
                                            [c for c in plan_entry.get("mid_phrase_changes", [])
                                             if 0 < c.get("at_beat", 0) <= beat_in_phrase],
                                            key=lambda x: x.get("at_beat", 0))
                                        for _pc in _past:
                                            _pb = _pc.get("at_beat", 0)
                                            self._mid_phrase_applied.add(
                                                (snap.track_id, phrase_idx, _pb))
                                        if _past:
                                            # Apply the LAST past change's values as current state
                                            _lc = _past[-1]
                                            if _lc.get("brightness") is not None:
                                                self.after(0, lambda b=_lc["brightness"]:
                                                           self._apply_ai_brightness(b))
                                            if _lc.get("speed") is not None:
                                                _sc = (_lc["speed"] * _bpm_scale
                                                       if _bpm_scale else _lc["speed"])
                                                self.after(0, lambda s=_sc:
                                                           self._apply_ai_speed(s))
                                            if _lc.get("direction"):
                                                self._apply_ai_direction(_lc["direction"])
                                            if _lc.get("grouping"):
                                                self._apply_ai_grouping(_lc["grouping"])
                                            if _lc.get("spacing") is not None:
                                                self._apply_ai_spacing(_lc["spacing"])
                                            if _lc.get("active_strips"):
                                                self._apply_ai_active_strips(
                                                    _lc["active_strips"])
                                            self._tlog_write("CATCHUP_FAST_FORWARD",
                                                f"phrase_idx={phrase_idx}  "
                                                f"beat_in_phrase={beat_in_phrase}  "
                                                f"applied_up_to_beat={_past[-1].get('at_beat')}  "
                                                f"n_past={len(_past)}")
                                else:
                                    in_flight = snap.track_id in self._ai_control._planning_tids
                                    self._tlog_write("HOLD_FOR_PLAN",
                                        f"deck=P{snap.player}  phrase={phrase}  "
                                        f"phrase_idx={phrase_idx}  "
                                        f"plan_in_flight={'YES' if in_flight else 'NO'}")
                                    # Always fall back to random so LEDs keep running.
                                    # plan_catch_up will override this when the plan arrives.
                                    if phrase:
                                        _fb_phrase = PHRASE_ALIAS.get(phrase, phrase)
                                        _fb_phrase = _fb_phrase if _fb_phrase.upper() in dict(CATEGORIES) else "intro"
                                        cat_name = _fb_phrase.upper()
                                        pool = dict(CATEGORIES).get(cat_name, []) or CATEGORIES[0][1]
                                        cur_cls = type(self._blt_phrase_inst) if self._blt_phrase_inst else None
                                        choices = [c for c in pool if c is not cur_cls] or pool
                                        self._blt_phrase_inst = random.choice(choices)()
                            else:
                                # WLED Control tab, or cue press / reroll: original logic
                                cat_name = PHRASE_ALIAS.get(phrase, phrase).upper()
                                pool     = dict(CATEGORIES).get(cat_name, []) or CATEGORIES[0][1]
                                cur_cls  = type(self._blt_phrase_inst) if self._blt_phrase_inst else None
                                choices  = [c for c in pool if c is not cur_cls] or pool
                                self._blt_phrase_inst = random.choice(choices)()
                                self._effect_start_time = time.perf_counter()
                                if phrase_changed:
                                    _default_bright = PHRASE_BRIGHTNESS.get(phrase, 0.7)
                                    # Randomise spacing, grouping and direction each phrase change
                                    _spc_pool = {
                                        "intro":  [8, 12, 18, 24],
                                        "verse":  [4, 6, 8, 12],
                                        "up":     [2, 4, 6, 8],
                                        "chorus": [0, 2, 4],
                                        "down":   [6, 8, 12],
                                        "outro":  [12, 18, 24],
                                    }
                                    _grp_pool = {
                                        "intro":  [1, 2, 3],
                                        "verse":  [1, 2, 3, 5],
                                        "up":     [1, 2, 3],
                                        "chorus": [1, 2],
                                        "down":   [1, 2, 3],
                                        "outro":  [1, 2, 3],
                                    }
                                    _dir_pool = {
                                        "intro":  ["forward", "reverse"],
                                        "verse":  ["forward", "reverse", "mirror"],
                                        "up":     ["forward", "mirror"],
                                        "chorus": ["mirror", "forward", "reverse"],
                                        "down":   ["reverse", "forward"],
                                        "outro":  ["forward", "reverse"],
                                    }
                                    _default_spacing  = random.choice(_spc_pool.get(phrase, [4, 8, 12]))
                                    _default_grouping = random.choice(_grp_pool.get(phrase, [1, 2, 3]))
                                    _default_dir      = random.choice(_dir_pool.get(phrase, ["forward", "reverse", "mirror"]))
                                    self.after(0, lambda b=_default_bright: self._bright_var.set(b))
                                    self._apply_ai_spacing(_default_spacing)
                                    self._apply_ai_grouping(_default_grouping)
                                    self._apply_ai_direction(_default_dir)
                                    _pal_name = self._palette_mgr.palettes[self._palette_mgr._active].name
                                    _osc_to_effect_r = ((time.perf_counter() - self._blt_reroll_time) * 1000
                                                        if self._blt_reroll_time > 0 else -1)
                                    self._ddp_log_pending = True
                                    self._tlog_write("EFFECT_APPLIED_RANDOM",
                                        f"phrase={phrase}  effect={type(self._blt_phrase_inst).__name__}  "
                                        f"palette={_pal_name}  bright={_default_bright:.2f}  "
                                        f"dir={_default_dir}  "
                                        f"group={self._ai_grouping}  spacing={_default_spacing}  "
                                        f"density={self._ai_density:.2f}  mode=RANDOM  "
                                        f"osc_to_effect={_osc_to_effect_r:.1f}ms")

                                # On reroll (not phrase_changed), reset brightness/speed to
                                # phrase defaults so each new effect starts at full energy,
                                # not at whatever drifted value the previous effect left behind.
                                if reroll_due and not ai_active and not phrase_changed:
                                    _reroll_bright = PHRASE_BRIGHTNESS.get(phrase, 0.7)
                                    self.after(0, lambda b=_reroll_bright:
                                               self._apply_ai_brightness(b))

                                # In AI mode, only swap palette on phrase change or cue press —
                                # never on bare reroll (AI owns palette mid-phrase)
                                swap_ok = (phrase_changed or cue_press
                                           or (reroll_due and not ai_active))
                                if swap_ok and not self._manual_palette:
                                    self.after(0, lambda p=phrase: self._swap_palette(p))

                            # ── UP → CHORUS impact flash ─────────────────────────
                            # Pre-arm already fired at beat boundary — OSC arrived after.
                            # Update pending effect with the planned chorus effect.
                            # If pre-arm hasn't fired yet, fire now (OSC arrived first).
                            if (phrase_changed
                                    and old_phrase == "up"
                                    and phrase == "chorus"
                                    and self._blt_phrase_inst is not None):
                                self._chorus_impact_fire_t    = None  # cancel any pending arm
                                self._chorus_impact_pre_armed = False
                                if type(self._blt_phrase_inst).__name__ == "ChorusImpactFlash":
                                    # Pre-arm already running — just register the planned effect
                                    self._chorus_impact_pending_inst = (
                                        self._chorus_impact_pending_inst or self._blt_phrase_inst)
                                    self._tlog_write("CHORUS_IMPACT_OSC_LATE",
                                        f"strobe already running, pending={type(self._chorus_impact_pending_inst).__name__}")
                                else:
                                    # OSC arrived first — fire impact now
                                    self._chorus_impact_pending_inst = self._blt_phrase_inst
                                    self._blt_phrase_inst = ChorusImpactFlash()
                                    self._effect_start_time = time.perf_counter()
                                    self._chorus_impact_active = True
                                    self._tlog_write("CHORUS_IMPACT_START",
                                        f"up→chorus  saved={type(self._chorus_impact_pending_inst).__name__}")

                            reason = ("phrase"  if phrase_changed else
                                      "cue"     if cue_press    else
                                      "catchup" if plan_catch_up else "reroll")
                            osc_lag_ms = ((time.perf_counter() - self._blt_reroll_time) * 1000
                                          if phrase_changed and self._blt_reroll_time > 0 else None)
                            lag_str = f"  osc→render={osc_lag_ms:.1f}ms" if osc_lag_ms is not None else ""
                            _log.info("SYNC  reason=%s  phrase=%r→%r  effect=%s  bn=%d  P%d%s",
                                      reason, old_phrase, phrase,
                                      type(self._blt_phrase_inst).__name__,
                                      beat_n, snap.player, lag_str)


                        # ChorusImpactFlash auto-expires after 1 bar (4 beats) → restore saved effect
                        if (self._blt_phrase_inst is not None
                                and type(self._blt_phrase_inst).__name__ == "ChorusImpactFlash"
                                and bpm > 0
                                and (time.perf_counter() - self._effect_start_time)
                                    > 4 * 60.0 / bpm):
                            if (self._chorus_impact_pending_inst is not None
                                    and type(self._chorus_impact_pending_inst).__name__
                                        != "ChorusImpactFlash"):
                                self._blt_phrase_inst = self._chorus_impact_pending_inst
                                self._chorus_impact_pending_inst = None
                            else:
                                self._blt_phrase_inst = self._pick_effect(phrase, None)
                                self._chorus_impact_pending_inst = None
                            self._chorus_impact_active = False
                            self._effect_start_time = time.perf_counter()
                            self._current_effect_name = type(self._blt_phrase_inst).__name__
                            # Sustain the peak energy after the strobe: keep beat overlay and
                            # push brightness/speed to ensure the chorus doesn't sag after impact.
                            _post_br = max(self._bright_var.get(), 0.90)
                            _post_sp = max(self._speed_var.get(), 1.5)
                            self.after(0, lambda b=_post_br, s=_post_sp: (
                                self._apply_ai_beat_overlay("beat"),
                                self._apply_ai_brightness(b),
                                self._apply_ai_speed(s),
                            ))
                            self._tlog_write("CHORUS_IMPACT_EXPIRED",
                                f"→ {self._current_effect_name}  forced_beat_overlay+peak_energy")

                        # Globe grid: render at ring_count resolution, tile across ring_size.
                        # Globe (any): render at min 300 so effects look correct, then resample.
                        _ring = dev.ring_render_size   # (ring_count, ring_size) or None
                        _is_globe_dev = dev.led_order is not None or _ring is not None
                        _render_n = (_ring[0] if _ring
                                     else (300 if _is_globe_dev and n < 300 else n))

                        if self._blt_phrase_inst:
                            try:
                                dev_px = self._render_effect(
                                    self._blt_phrase_inst, t, bpm, _render_n,
                                    self._palette_mgr.active.color, speed, dev)
                            except Exception as e:
                                _log.warning("BLT effect render failed (%s): %s",
                                             type(self._blt_phrase_inst).__name__, e)
                                self._blt_phrase_inst = None   # force re-roll next frame
                                dev_px = np.zeros((_render_n, 3), dtype=np.uint8)
                        else:
                            dev_px = np.zeros((_render_n, 3), dtype=np.uint8)

                        # Resample from virtual 300 → actual n for non-grid globe
                        if not _ring and _render_n != n:
                            _idx = np.linspace(0, _render_n - 1, n).astype(np.int32)
                            dev_px = dev_px[_idx]

                        # Tile ring pixels across all LEDs in each ring
                        if _ring:
                            ring_count, ring_size = _ring
                            dev_px = np.repeat(dev_px, ring_size, axis=0)

                else:
                    # No playing deck — hold the last effect frozen rather than going dark
                    _ring = dev.ring_render_size
                    _is_globe_dev = dev.led_order is not None or _ring is not None
                    _render_n = (_ring[0] if _ring
                                 else (300 if _is_globe_dev and n < 300 else n))
                    if self._blt_phrase_inst:
                        _hold_bpm = getattr(self, "_last_blt_bpm", bpm) or bpm
                        try:
                            dev_px = self._render_effect(
                                self._blt_phrase_inst, t, _hold_bpm, _render_n,
                                self._palette_mgr.active.color, speed, dev)
                        except Exception:
                            dev_px = np.zeros((_render_n, 3), dtype=np.uint8)
                    else:
                        dev_px = np.zeros((_render_n, 3), dtype=np.uint8)
                    if not _ring and _render_n != n:
                        _idx = np.linspace(0, _render_n - 1, n).astype(np.int32)
                        dev_px = dev_px[_idx]
                    if _ring:
                        dev_px = np.repeat(dev_px, _ring[1], axis=0)

                # ── Globe remap ─────────────────────────────────────────
                # For grid globe: ring tiling already handled above, skip led_order remap.
                # For other globe types: reorder by sort_axis.
                _ring_handled = dev.ring_render_size is not None
                _led_order = None if _ring_handled else dev.led_order
                _is_globe  = dev.ring_render_size is not None or dev.led_order is not None
                if _led_order is not None:
                    _remapped = np.empty_like(dev_px)
                    _remapped[_led_order] = dev_px
                    dev_px = _remapped

                # ── Vault generative renderer ────────────────────────────
                # Runs before AI post-processing so grouping/spacing/direction
                # all apply to vault output — same pipeline as regular effects.
                if not _is_globe and self._vault_gen_enabled and self._vault_renderer is not None:
                    _vg_bpm  = bpm
                    _vg_snap = self._sb_snap_cache
                    if _vg_snap is not None and _vg_snap.bpm:
                        _vg_bpm = _vg_snap.bpm
                    _bp = (t * _vg_bpm / 60.0) % 1.0

                    _vg_entry = None

                    # Case 1: manual vault pattern button selected
                    if self._vault_forced_pattern:
                        # Pull c1/c2 from the active palette at 0 and 0.75
                        try:
                            _pc1 = list(self._palette_mgr.active.color(0.0))
                            _pc2 = list(self._palette_mgr.active.color(0.75))
                        except Exception:
                            _pc1 = [220, 80, 10]; _pc2 = [10, 10, 60]
                        _vg_entry = {
                            "pattern":         self._vault_forced_pattern,
                            "color_primary":   _pc1,
                            "color_secondary": _pc2,
                            "speed":           float(self._speed_var.get()),
                            "brightness":      float(self._bright_var.get()),
                            "coverage":        0.65,
                            "size":            0.22,
                            "beat_sync":       "beat",
                        }

                    # Case 2: AI vault planner has a plan
                    elif (self._ai_control is not None
                            and getattr(self._ai_control, '_vault_planner', None) is not None
                            and _vg_snap is not None and _vg_snap.is_playing
                            and self._ai_control._vault_planner.has_plan(_vg_snap.track_id)):
                        _vg_entry = self._ai_control._vault_planner.get_entry(
                            _vg_snap.track_id, self._blt_phrase_idx_last)

                    if _vg_entry is not None:
                        try:
                            dev_px = self._vault_renderer.render(t, _vg_bpm, _vg_entry, _bp)
                        except Exception:
                            pass  # fallback: keep regular effect output
                # ──────────────────────────────────────────────────────────

                # ── Advanced DDPCanvas persistent-buffer engine ───────────
                # Runs before AI post-processing so grouping/spacing/direction
                # apply to the output — same pipeline position as vault.
                if self._adv_gen_enabled and self._adv_renderer is not None \
                        and self._adv_forced_pattern:
                    _adv_bpm = bpm
                    _adv_snap = self._sb_snap_cache
                    if _adv_snap is not None and _adv_snap.bpm:
                        _adv_bpm = _adv_snap.bpm
                    _adv_bp = (t * _adv_bpm / 60.0) % 1.0
                    try:
                        _pc1 = list(self._palette_mgr.active.color(0.0))
                        _pc2 = list(self._palette_mgr.active.color(0.75))
                    except Exception:
                        _pc1 = [255, 140, 20]; _pc2 = [20, 80, 220]
                    _adv_params = {
                        "pattern":         self._adv_forced_pattern,
                        "n":               n,
                        "color_primary":   _pc1,
                        "color_secondary": _pc2,
                        "speed":           float(self._speed_var.get()),
                        "brightness":      float(self._bright_var.get()),
                    }
                    try:
                        dev_px = self._adv_renderer.render(t, _adv_bpm, _adv_params, _adv_bp)
                    except Exception:
                        pass  # fallback: keep regular effect output
                # ──────────────────────────────────────────────────────────

                # ── AI post-processing ──────────────────────────────────
                # 1. Direction
                d = self._ai_direction
                if d == "reverse":
                    dev_px = dev_px[::-1].copy()
                elif d == "mirror":
                    dev_px = dev_px.copy()
                    half = n // 2
                    dev_px[half:half * 2] = dev_px[:half][::-1]

                # 2+3. Grouping/spacing/offset handled in _apply_strip_transforms
                #       (called below, per device geometry)

                # 4. Density (uniform thinning)
                density = self._ai_density
                if density < 0.999:
                    thin_step = max(1, round(1.0 / density))
                    thin_mask = np.zeros(n, dtype=bool)
                    thin_mask[::thin_step] = True
                    if not dev_px.flags['WRITEABLE']:
                        dev_px = dev_px.copy()
                    dev_px[~thin_mask] = 0
                # 5. Active strips — zero out inactive strips (snake rig only, not globe)
                _active = self._ai_active_strips
                if not _is_globe and len(_active) < 5:
                    _lps = n // 5   # LEDs per strip
                    if not dev_px.flags['WRITEABLE']:
                        dev_px = dev_px.copy()
                    for _s in range(5):
                        if (_s + 1) not in _active:
                            dev_px[_s * _lps : (_s + 1) * _lps] = 0
                # ────────────────────────────────────────────────────────

                # Globe: enforce minimum brightness so sphere is always visible
                _GLOBE_BRIGHT_FLOOR = 0.15
                _eff_brightness = (max(brightness, _GLOBE_BRIGHT_FLOOR)
                                   if _is_globe else brightness)
                if _eff_brightness < 0.999:
                    dev_px = (dev_px.astype(np.float32) * _eff_brightness).clip(0, 255).astype(np.uint8)

                # Smooth fade-in on effect start (300ms)
                _eff_age = time.perf_counter() - self._effect_start_time
                if _eff_age < 0.3:
                    _fade = _eff_age / 0.3
                    dev_px = (dev_px.astype(np.float32) * _fade).clip(0, 255).astype(np.uint8)

                # Beat overlay — bar = brightness boost, beat/2beat = colored sparks
                _overlay_mode = self._ai_beat_overlay
                if (_overlay_mode != "off"
                        and phrase not in ("intro", "down", "outro")):
                    _now_ov = time.perf_counter()

                    if _overlay_mode == "bar":
                        # Clear any leftover sparks from a previous mode
                        if self._sparks:
                            self._sparks = []
                        # Brightness pulse on bar start — additive, doesn't reset animation
                        if self._last_bar_t > 0:
                            _age = _now_ov - self._last_bar_t
                            if _age < 0.18:
                                # Additive brightness boost: peaks at +20%, decays in 180ms
                                _pulse = 0.20 * math.exp(-_age * 18.0)
                                dev_px = np.clip(
                                    dev_px.astype(np.float32) * (1.0 + _pulse), 0, 255
                                ).astype(np.uint8)

                    else:  # "beat" or "2beat"
                        try:
                            _trig_n = (self._last_2beat_beat_n if _overlay_mode == "2beat"
                                       else self._last_beat_n)
                            _trig_t = (self._last_2beat_t if _overlay_mode == "2beat"
                                       else self._last_beat_t)

                            # Additive brightness pulse on beat — does NOT reset animation.
                            # The running effect continues underneath; pulse adds on top.
                            if _trig_t > 0:
                                _beat_age = _now_ov - _trig_t
                                if _beat_age < 0.20:
                                    # Sharp exponential decay: +30% at t=0, gone by 200ms
                                    _pulse = 0.30 * math.exp(-_beat_age * 20.0)
                                    dev_px = np.clip(
                                        dev_px.astype(np.float32) * (1.0 + _pulse), 0, 255
                                    ).astype(np.uint8)

                            # Spawn colored sparks on top of the pulse (additive)
                            if _trig_n != self._sparks_trigger_n and _trig_n >= 0:
                                self._sparks_trigger_n = _trig_n
                                _sp_r, _sp_g, _sp_b = self._palette_mgr.active.color(
                                    float(np.random.random()))
                                _sp_count = np.random.randint(6, 12)
                                _sp_pos = np.random.choice(n, size=_sp_count, replace=False)
                                self._sparks.append((_sp_pos, _sp_r, _sp_g, _sp_b, _trig_t))

                            # Render sparks with quadratic fade over 150ms
                            if self._sparks:
                                _spark_window = 0.15
                                _dev_f = dev_px.astype(np.float32)
                                _remaining = []
                                for _sp_pos, _sp_r, _sp_g, _sp_b, _sp_t in self._sparks:
                                    _age = _now_ov - _sp_t
                                    if _age < _spark_window:
                                        _remaining.append((_sp_pos, _sp_r, _sp_g, _sp_b, _sp_t))
                                        _fade = (1.0 - _age / _spark_window) ** 2
                                        _valid = _sp_pos[_sp_pos < n]
                                        if len(_valid) == 0:
                                            continue
                                        _dev_f[_valid, 0] = np.clip(
                                            _dev_f[_valid, 0] + _sp_r * 255 * _fade, 0, 255)
                                        _dev_f[_valid, 1] = np.clip(
                                            _dev_f[_valid, 1] + _sp_g * 255 * _fade, 0, 255)
                                        _dev_f[_valid, 2] = np.clip(
                                            _dev_f[_valid, 2] + _sp_b * 255 * _fade, 0, 255)
                                self._sparks = _remaining
                                dev_px = _dev_f.clip(0, 255).astype(np.uint8)
                        except Exception as _spark_exc:
                            _log.warning("spark overlay error (sparks cleared): %s", _spark_exc)
                            self._sparks = []

                # ── Globe generative renderer (beta) ────────────────────
                if (_is_globe and self._globe_renderer is not None
                        and self._ai_control is not None
                        and getattr(self._ai_control, '_globe_planner', None) is not None):
                    _g_snap = self._sb_snap_cache
                    if (_g_snap is not None and _g_snap.is_playing
                            and self._ai_control._globe_planner.has_plan(_g_snap.track_id)):
                        _g_pidx  = self._blt_phrase_idx_last
                        _g_entry = self._ai_control._globe_planner.get_entry(
                            _g_snap.track_id, _g_pidx)
                        if _g_entry is not None:
                            _g_bpm      = _g_snap.bpm or bpm
                            _beat_phase = (t * _g_bpm / 60.0) % 1.0
                            try:
                                _g_entry = dict(_g_entry)
                                # Apply mid-phrase changes that have passed
                                _g_mpc = _g_entry.get("mid_phrase_changes", [])
                                if _g_mpc:
                                    _g_bip = beat_in_phrase
                                    for _mpc in sorted(_g_mpc, key=lambda x: x.get("at_beat", 999)):
                                        if _g_bip >= _mpc.get("at_beat", 999):
                                            _g_entry.update({k: v for k, v in _mpc.items()
                                                             if k != "at_beat"})
                                # Override plan colours with the active palette
                                _g_entry["color_primary"]   = list(self._palette_mgr.active.color(0.0))
                                _g_entry["color_secondary"] = list(self._palette_mgr.active.color(0.6))
                            except Exception:
                                pass
                            try:
                                dev_px = self._globe_renderer.render(
                                    t, _g_bpm, _g_entry, _beat_phase)
                            except Exception:
                                pass  # keep existing dev_px on failure
                # ── Grouping / spacing / offset — apply to main strip output ─
                # _is_globe is set above (line ~4413). Skip globes and back strip.
                if not _is_globe and dev.geometry.get("type") in ("snake", "linear"):
                    try:
                        self._apply_strip_transforms(dev_px, dev, t, bpm)
                    except Exception as _xf_e:
                        _log.debug("strip transform error: %s", _xf_e)

                # ── Back strip — always-on, max brightness, block patterns ──
                # Always routed through CaveStripRenderer (bright + blocky).
                # AI plan back_accent overrides phrase; "off" → dark.
                _is_back_strip = (
                    not _is_globe
                    and dev.geometry.get("type") == "linear"
                    and dev.led_count >= 500
                )
                if _is_back_strip:
                    _bs_snap     = self._sb_snap_cache
                    _bs_bpm      = ((_bs_snap.bpm if _bs_snap else 0) or bpm or 125.0)
                    _bs_beat_n   = getattr(_bs_snap, "beat_number", 0) or 0
                    _bs_beat_age = (time.perf_counter() - self._last_beat_t
                                    if self._last_beat_t > 0 else 999.0)
                    _bs_c1 = np.array(self._palette_mgr.active.color(0.0),
                                      dtype=np.float32) / 255.0
                    _bs_c2 = np.array(self._palette_mgr.active.color(0.65),
                                      dtype=np.float32) / 255.0

                    # Resolve back_accent from AI plan / choreo_mgr
                    _bs_accent = None
                    if self._ai_control:
                        _bs_plan = self._ai_control._track_plans.get(
                            _bs_snap.track_id if _bs_snap else 0)
                        if _bs_plan:
                            _bs_bn = _bs_snap.beat_number or 0 if _bs_snap else 0
                            _bs_entry = None
                            for _bpi in sorted(_bs_plan.keys()):
                                _bpe = _bs_plan[_bpi]
                                if (_bpe.get("beat_start", 0) <= _bs_bn
                                        <= _bpe.get("beat_end", 99999)):
                                    _bs_entry = _bpe
                            if _bs_entry:
                                _bs_bip = (_bs_bn - _bs_entry.get("beat_start", 0))
                                for _bmc in sorted(_bs_entry.get("mid_phrase_changes", []),
                                                   key=lambda x: x.get("at_beat", 999)):
                                    if _bmc.get("at_beat", 999) <= _bs_bip:
                                        if "back_trigger" in _bmc:
                                            _bs_accent = _bmc["back_trigger"]
                                if _bs_accent is None:
                                    _bs_accent = _bs_entry.get("back_accent")
                    if _bs_accent is None:
                        _bs_accent = getattr(self._choreo_mgr, "_current_back_accent", None)
                    # Default: only fire on up/chorus — dark otherwise
                    if _bs_accent is None:
                        _bs_accent = {"up": "pulse_build", "chorus": "impact_flash"}.get(
                            phrase, "off")

                    if _bs_accent == "off":
                        dev_px = np.zeros((dev.led_count, 3), dtype=np.uint8)
                    elif self._cave_strip_renderer is not None:
                        _cs_phrase = {"impact_flash": "chorus",
                                      "pulse_build":  "up",
                                      "strobe_burst": "strobe"}.get(
                                          _bs_accent, phrase or "up")
                        try:
                            dev_px = self._cave_strip_renderer.render(
                                t=t, bpm=_bs_bpm, phrase=_cs_phrase,
                                beat_n=_bs_beat_n, beat_age=_bs_beat_age,
                                c1=(_bs_c1 * 255).astype(np.uint8),
                                c2=(_bs_c2 * 255).astype(np.uint8))
                        except Exception:
                            dev_px = np.zeros((dev.led_count, 3), dtype=np.uint8)
                    else:
                        dev_px = np.zeros((dev.led_count, 3), dtype=np.uint8)

                    self._last_back_px = dev_px
                # ─────────────────────────────────────────────────────────


                _master = self._master_bright_var.get()
                if _master < 0.999:
                    dev_px = (dev_px.astype(np.float32) * _master).clip(0, 255).astype(np.uint8)
                # Global spatial bloom — applied to ALL effects before DDP send and preview
                try:
                    dev_px = _spatial_bloom(dev_px)
                except Exception as _bloom_exc:
                    _log.warning("bloom error: %s", _bloom_exc)
                px_devs.append((dev, dev_px))

            # Send to all devices in parallel so they light up simultaneously
            self._sender.send_parallel([(px, d.ip) for d, px in px_devs])
            if self._ddp_log_pending and px_devs:
                self._ddp_log_pending = False
                _osc_to_ddp = ((time.perf_counter() - self._blt_reroll_time) * 1000
                               if self._blt_reroll_time > 0 else -1)
                self._tlog_write("DDP_SENT",
                    f"osc_to_ddp={_osc_to_ddp:.1f}ms  ips={[d.ip for d, _ in px_devs]}")

            with self._px_lock:
                self._last_px_devs = px_devs
                # _last_px = first device's pixels, used for strip previews
                self._last_px = px_devs[0][1] if px_devs else np.zeros((1, 3), dtype=np.uint8)

        _log.error("RENDER_THREAD_EXITED  running=%s", self._running)

    def _render_frame_safe(self):
        """Called from _render_loop; if this raises, the loop catches and logs it."""
        pass  # body moved inline; this stub is unused

    # ── AI helpers ────────────────────────────────────────────────────────────

    def _blt_force_dump(self, player_num: int):
        """Ask BLT via nREPL to re-dump track metadata for a player that's missing phrases.

        Resets [:vp-players dev :last-dump] to 0 so the periodic re-dump fires
        on the next beat, then calls vp-dump-meta! directly for immediate effect.
        """
        import socket as _sock
        def be(txt):
            b = txt.encode('utf-8')
            return f'{len(b)}:'.encode() + b
        code = (
            f"(in-ns 'beat-link-trigger.expressions.triggers) "
            f"(swap! globals assoc-in [:vp-players (int {player_num}) :last-dump] 0) "
            f"(future (try (vp-dump-meta! (int {player_num}) nil) "
            f"             (catch Exception e (println \"[VP] force-dump err:\" (.getMessage e)))))"
        )
        try:
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            sock.connect(('127.0.0.1', 17001))
            sock.settimeout(3)
            msg = b'd' + be('op') + be('eval') + be('code') + be(code) + be('id') + be('r') + b'e'
            sock.sendall(msg)
            import time as _t; _t.sleep(0.2)
            sock.close()
            _log.info("BLT nudge: forced metadata re-dump for P%d", player_num)
        except Exception as e:
            _log.debug("BLT force-dump P%d failed: %s", player_num, e)

    def _tlog_write(self, event, detail=""):
        """Write one line to the timing log (called from any thread)."""
        try:
            ms = (time.perf_counter() - self._tlog_t0) * 1000
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._tlog.write(f"{ts}  {ms:10.1f}  {event:<28s}  {detail}\n")
            _log.info("TLOG  %s  %s", event, detail)
        except Exception:
            pass

    def _pick_effect(self, phrase, ai_name=None):
        """Return an instantiated effect class. Accept ai_name from any pool — no pool restriction."""
        phrase = PHRASE_ALIAS.get(phrase, phrase)
        if ai_name:
            cat_dict = dict(CATEGORIES)
            cat_name = phrase.upper()
            # Search phrase pool first, then ADVANCED, then all other pools.
            # VAULT only included when vault setup is active.
            _vault_ok = self._device_mgr.active_setup == "vault"
            other_cats = [c for c in cat_dict if c not in (cat_name, "VAULT", "ADVANCED")]
            search_pools = ([cat_dict.get(cat_name, [])]
                            + ([cat_dict.get("VAULT", [])] if _vault_ok else [])
                            + [cat_dict.get("ADVANCED", [])]
                            + [cat_dict.get(c, []) for c in other_cats])
            for pool in search_pools:
                for cls in pool:
                    if cls.__name__ == ai_name:
                        return cls()
            # AI gave a completely unknown effect name — log and fall through to random
            self._tlog_write("EFFECT_UNKNOWN",
                f"ai_name={ai_name!r}  phrase={phrase}  → not found in any pool, falling back to random")
        cat_name = phrase.upper()
        pool     = dict(CATEGORIES).get(cat_name, []) or CATEGORIES[0][1]
        cur_cls  = type(self._blt_phrase_inst) if self._blt_phrase_inst else None
        choices  = [c for c in pool if c is not cur_cls] or pool
        return random.choice(choices)()

    def _end_chorus_impact(self):
        """Restore the real chorus effect after the 4-beat impact flash."""
        if self._chorus_impact_pending_inst is not None:
            self._blt_phrase_inst = self._chorus_impact_pending_inst
            self._chorus_impact_pending_inst = None
        self._chorus_impact_active = False

    def _apply_ai_palette(self, palette_name, phrase=None):
        """Set active palette by name. Any known palette is accepted regardless of phrase pool.
        Rejects same-as-current to enforce variety."""
        if self._manual_palette:
            return   # user has manually chosen a palette — AI cannot override until next phrase
        if palette_name:
            # Skip if it's already the active palette — force variety
            current_name = self._palette_mgr.active.name
            if palette_name == current_name:
                self.after(0, lambda p=phrase: self._swap_palette(p))
                return
            for i, p in enumerate(self._palette_mgr.palettes):
                if p.name == palette_name:
                    self._palette_mgr.set_active(i)
                    self.after(0, self._refresh_palettes)
                    return
            # Name not found — log and fall through to random
            self._tlog_write("PALETTE_NAME_UNKNOWN",
                f"ai_name={palette_name!r}  phrase={phrase}  → falling back to random")
        # Name not found or None — phrase-aware random swap
        self.after(0, lambda p=phrase: self._swap_palette(p))

    def _apply_ai_brightness(self, brightness):
        """Set brightness slider from AI decision."""
        if brightness is not None:
            self._bright_var.set(float(max(0.08, min(1.0, brightness))))

    def _apply_ai_speed(self, speed):
        """Set speed slider from AI decision. Hard cap at 0.5."""
        if speed is not None:
            self._speed_var.set(float(max(0.10, min(0.50, speed))))

    def _apply_ai_direction(self, direction):
        """Set LED direction from AI decision ('forward', 'reverse', or 'mirror')."""
        if direction in ("forward", "reverse", "mirror"):
            self._ai_direction = direction
            _log.info("DIRECTION  %s", direction)

    def _apply_ai_grouping(self, grouping, manual=False):
        """Set LED grouping. manual=True: always apply + lock. False: skip if locked."""
        if grouping is None:
            return
        if not manual and self._manual_grouping:
            return   # user has overridden — AI cannot change until next phrase
        self._ai_grouping = int(max(1, min(10, grouping)))
        _log.info("GROUPING  %d  manual=%s", self._ai_grouping, manual)
        if manual:
            self._manual_grouping = True
            # Push into choreo_mgr so it takes effect on the pixel render
            # Also set the manual flag there so _apply_plan_entry won't override it
            if self._choreo_mgr is not None:
                self._choreo_mgr._current_grouping = self._ai_grouping
                self._choreo_mgr._manual_grouping  = True
        _seg = getattr(self, "_grouping_seg", None)
        if _seg:
            nearest = min(["1","2","3","5","8"], key=lambda v: abs(int(v) - self._ai_grouping))
            try: _seg.set(nearest)
            except Exception: pass

    def _apply_ai_spacing(self, spacing, manual=False):
        """Set LED spacing. manual=True: always apply + lock. False: skip if locked."""
        if spacing is None:
            return
        if not manual and self._manual_spacing:
            return   # user has overridden — AI cannot change until next phrase
        self._ai_spacing = int(max(0, min(30, spacing)))
        _log.info("SPACING  %d  manual=%s", self._ai_spacing, manual)
        if manual:
            self._manual_spacing = True
            # Push into choreo_mgr so it takes effect on the pixel render
            # Also set the manual flag there so _apply_plan_entry won't override it
            if self._choreo_mgr is not None:
                self._choreo_mgr._current_spacing = self._ai_spacing
                self._choreo_mgr._manual_spacing  = True
        _seg = getattr(self, "_spacing_seg", None)
        if _seg:
            nearest = min(["0","2","4","8","16"], key=lambda v: abs(int(v) - self._ai_spacing))
            try: _seg.set(nearest)
            except Exception: pass

    def _apply_ai_offset(self, offset):
        """Set LED offset from AI decision (0.0–1.0)."""
        if offset is not None:
            self._ai_offset = float(offset) % 1.0
            _log.info("OFFSET  %.3f", self._ai_offset)

    def _apply_ai_density(self, density):
        """Set LED density from AI decision (0.2–1.0)."""
        if density is not None:
            self._ai_density = float(max(0.2, min(1.0, density)))

    def _apply_ai_beat_overlay(self, mode):
        """Set beat overlay mode from AI decision ('bar', 'beat', '2beat', 'off')."""
        if mode in ("bar", "beat", "2beat", "off"):
            self._ai_beat_overlay = mode

    def _apply_ai_render_mode(self, mode):
        """Set render mode from AI decision ('full', 'tile', 'cascade')."""
        if mode in ("full", "tile", "cascade"):
            self._ai_render_mode = mode
            self.after(0, self._update_render_mode_btn)

    def _apply_ai_active_strips(self, strips):
        """Set which strips are lit from AI decision (list of 1-based strip numbers)."""
        if strips and isinstance(strips, list):
            valid = [s for s in strips if isinstance(s, int) and 1 <= s <= 5]
            self._ai_active_strips = valid if valid else [1, 2, 3, 4, 5]

    def _update_render_mode_btn(self):
        """Refresh render mode button label on main thread."""
        if hasattr(self, "_sb_render_btn"):
            self._sb_render_btn.configure(text=self._ai_render_mode.upper())

    def _apply_strip_transforms(self, dev_px, dev, t=0.0, bpm=120.0):
        """Apply grouping/spacing comb-filter and per-strip offset to dev_px in-place.

        The comb filter scans one period per beat so grouping is visually obvious.
        Only acts when grouping > 1 or spacing > 0 or offset != 0.
        """
        grp = int(self._ai_grouping)
        spc = int(self._ai_spacing)
        off = float(self._ai_offset)
        if grp < 1:
            grp = 1

        geom = dev.geometry if dev else {}
        if geom.get("type") == "snake":
            lps  = int(geom.get("leds_per_segment", 300))
            segs = dev.led_count // lps if lps > 0 else 1
        else:
            lps  = dev.led_count
            segs = 1

        period = max(1, grp + spc)

        for s in range(segs):
            lo = s * lps
            hi = min(lo + lps, dev.led_count)
            seg_len = hi - lo

            # Offset — circular shift per segment (alternating direction for snake)
            off_leds = int(round(off * lps)) % lps if lps > 0 else 0
            if s % 2 == 1:
                off_leds = lps - off_leds   # reverse direction on odd segments
            if off_leds:
                dev_px[lo:hi] = np.roll(dev_px[lo:hi], off_leds, axis=0)

            # Grouping/spacing comb filter — static mask, variable block widths
            # per strip so each strip looks different. Position is fixed (no beat
            # scan) so the underlying animation flows smoothly through the windows.
            if spc > 0 or grp > 1:
                rng  = np.random.RandomState(s * 997 + grp * 31 + spc * 7)
                mask = np.zeros(seg_len, dtype=bool)
                p = 0
                while p < seg_len:
                    on  = max(1, int(rng.uniform(max(1, grp * 0.5), grp * 1.8 + 1)))
                    gap = max(0, int(rng.uniform(max(0, spc * 0.4), spc * 1.8 + 1)))
                    hi2 = min(seg_len, p + on)
                    if hi2 > p:
                        mask[p:hi2] = True
                    p += on + gap
                dev_px[lo:hi][~mask] = 0

    def _render_effect(self, inst, t, bpm, n, palette_fn, speed, dev):
        """Render effect in current render_mode, tiling across segments for snake layouts."""
        mode = self._ai_render_mode
        if mode == "full":
            return inst.render(t, bpm, n, palette_fn, speed)

        # Tile and cascade modes: work per segment
        geom = dev.geometry if dev else {}
        seg_n = int(geom.get("leds_per_segment", 300)) if geom.get("type") == "snake" else n
        segs  = n // seg_n if seg_n > 0 else 1

        if segs <= 1:
            return inst.render(t, bpm, n, palette_fn, speed)

        frames = []
        for i in range(segs):
            # cascade: offset each segment's time by half a beat per strip
            t_seg = t + (i * (60.0 / bpm) * 0.5) if mode == "cascade" else t
            frame = inst.render(t_seg, bpm, seg_n, palette_fn, speed)
            # Snake: reverse every other segment to match physical wiring
            frames.append(frame[::-1] if i % 2 == 1 else frame)

        return np.concatenate(frames, axis=0)

    def _load_session_memory(self):
        """Load persisted phrase history from previous session (last 8 entries)."""
        try:
            with open(self._session_memory_path, "r") as f:
                data = json.load(f)
                history = data.get("phrase_history", [])
                # Keep only valid entries
                valid = [e for e in history
                         if isinstance(e, dict) and "phrase" in e
                         and "effect" in e and "palette" in e]
                return valid[-8:]
        except Exception:
            return []

    def _load_prefs(self):
        """Load persistent user preferences (master brightness, etc.)."""
        try:
            with open(self._PREFS_PATH) as f:
                prefs = json.load(f)
            mb = float(prefs.get("master_brightness", 1.0))
            self._master_bright_var.set(max(0.0, min(1.0, mb)))
        except Exception:
            pass  # First run or corrupt file — use defaults

    def _save_prefs(self):
        """Persist user preferences to disk."""
        try:
            prefs = {
                "master_brightness": round(self._master_bright_var.get(), 4),
            }
            with open(self._PREFS_PATH, "w") as f:
                json.dump(prefs, f, indent=2)
        except Exception:
            pass

    def _save_session_memory(self):
        """Persist current phrase history to disk for next session."""
        try:
            with open(self._session_memory_path, "w") as f:
                json.dump({"phrase_history": self._phrase_history}, f, indent=2)
        except Exception:
            pass

    def _record_phrase_history(self, phrase, effect, palette):
        """Append to rolling phrase history (max 20 entries, persisted across sessions)."""
        self._phrase_history.append({"phrase": phrase, "effect": effect, "palette": palette})
        if len(self._phrase_history) > 20:
            self._phrase_history.pop(0)

    def _phrase_history_summary(self):
        """One-line summary of recent phrase history for AI context."""
        if not self._phrase_history:
            return "none"
        return "  |  ".join(
            f"{e['phrase']}:{e['effect']}({e['palette']})"
            for e in self._phrase_history
        )

    # ── UI refresh (main thread, 20 fps) ─────────────────────────────────────

    def _schedule_ui_refresh(self):
        try:
            self._ui_refresh()
        except Exception as e:
            _log.error("UI refresh error: %s", e, exc_info=True)
        finally:
            self.after(33, self._schedule_ui_refresh)  # always reschedule

    def _ui_refresh(self):
        with self._px_lock:
            px_devs = list(self._last_px_devs)
            px      = self._last_px.copy()

        # Sync BPM display with live CDJ/BLT when active (no manual effect playing)
        if (PRODJ_OK or BLT_OK) and not self._active_inst:
            try:
                best = self._best_cdj_client()
                if best:
                    raw = best.bpm
                    if isinstance(raw, (int, float)) and raw > 0:
                        pitch = getattr(best, "actual_pitch", 1.0) or 1.0
                        live_bpm = round(raw * pitch, 1)
                        if abs(live_bpm - self._bpm_var.get()) > 0.05:
                            self._bpm_var.set(live_bpm)
                            self._bpm_str.set(str(live_bpm))
            except Exception:
                pass

        # Always keep snap cache fresh from UI thread so status bar + AI tab work
        # regardless of whether blt_beats_live is True in the render thread.
        if BLT_OK and self._blt:
            try:
                _snap = self._best_blt_snap_for_ui()
                if _snap is not None:
                    self._sb_snap_cache = _snap
            except Exception:
                pass
            # Scan all 4 decks: fire notify_track_loaded when any deck's track changes.
            # Each deck is isolated — one deck's exception cannot kill the others.
            if self._ai_control:
                _now = time.time()
                for _p_num in range(1, 4):
                    try:
                        _p_snap = self._blt.get_snapshot(_p_num)
                        _tid = _p_snap.track_id
                        _url = _p_snap.waveform_url
                        _has_phrases = bool(_p_snap.phrases)
                        _has_cues    = bool(_p_snap.cues)
                        _has_analysis = bool(
                            self._ai_control._track_analysis_cache.get(_tid))
                        _prev_tid = self._ai_wv_tids.get(_p_num)
                        _prev_url = self._ai_wv_urls.get(_p_num)
                        _tid_changed = _tid and _tid != _prev_tid
                        _url_changed = _url and _url != _prev_url

                        # Detect phrases arriving after track was already registered
                        _phrases_arrived = (
                            _has_phrases
                            and _tid
                            and _tid == _prev_tid
                            and self._ai_phrases_have.get(_p_num) != _tid
                        )
                        # Detect waveform analysis arriving for a no-phrases/no-cues track
                        _analysis_arrived = (
                            _has_analysis
                            and not _has_phrases and not _has_cues
                            and _tid
                            and _tid == _prev_tid
                            and self._ai_phrases_have.get(_p_num) != _tid
                        )

                        if not (_tid_changed or _url_changed or _phrases_arrived
                                or _analysis_arrived):
                            _has_plan = bool(
                                self._ai_control and
                                self._ai_control._track_plans.get(_tid))
                            # Retry whenever phrases are missing — regardless of plan status.
                            # A synthetic plan may exist but we still want real phrases.
                            if _tid and not _has_phrases and not _has_analysis:
                                _pending = self._ai_phrases_pending.get(_p_num)
                                if _pending and _pending[0] == _tid:
                                    _wait_s = _now - _pending[1]
                                    # UI warning only shown when no plan covers the gap
                                    if _wait_s > 3.0 and not _has_plan:
                                        _lbl = self._ai_control._deck_plan_status_lbl.get(_p_num)
                                        if _lbl:
                                            _msg = (f"Waiting for track data… {int(_wait_s)}s"
                                                    if _wait_s < 30
                                                    else "No phrases — check ATF/rekordbox")
                                            _col = "orange" if _wait_s < 30 else "red"
                                            self.after(0, lambda l=_lbl, m=_msg, c=_col:
                                                       l.configure(text=m, text_color=c))
                                    _logged = self._ai_phrases_logged.get(_p_num, set())
                                    # Nudge BLT at 3s and 10s regardless of plan status
                                    for _retry_s in (3, 10):
                                        if _wait_s >= _retry_s and _retry_s not in _logged:
                                            _logged.add(_retry_s)
                                            self._ai_phrases_logged[_p_num] = _logged
                                            self._tlog_write("PHRASES_RETRY",
                                                f"deck=P{_p_num}  track_id={_tid}  wait={_wait_s:.1f}s  nudging_BLT")
                                            threading.Thread(
                                                target=self._blt_force_dump,
                                                args=(_p_num,), daemon=True).start()
                                            break
                                    for _ms in (3, 10, 30):
                                        if _wait_s >= _ms and _ms not in _logged:
                                            self._tlog_write("PHRASES_PENDING",
                                                f"deck=P{_p_num}  track_id={_tid}  wait={_wait_s:.1f}s")
                                            _logged.add(_ms)
                                            self._ai_phrases_logged[_p_num] = _logged
                                            break
                                else:
                                    self._ai_phrases_pending[_p_num] = (_tid, _now)
                                    self._ai_phrases_logged[_p_num] = set()
                                    self._tlog_write("PHRASES_WAIT_START",
                                        f"deck=P{_p_num}  track_id={_tid}")
                            else:
                                self._ai_phrases_pending.pop(_p_num, None)
                            continue

                        self._ai_wv_tids[_p_num] = _tid
                        self._ai_wv_urls[_p_num]  = _url
                        if _has_phrases:
                            self._ai_phrases_have[_p_num] = _tid
                            self._ai_phrases_logged.pop(_p_num, None)
                            _was_pending = self._ai_phrases_pending.pop(_p_num, None)
                            if _was_pending and _was_pending[0] == _tid:
                                _waited = _now - _was_pending[1]
                                self._tlog_write("PHRASES_ARRIVED",
                                    f"deck=P{_p_num}  track_id={_tid}  waited={_waited:.1f}s"
                                    + ("  trigger=LATE" if not _tid_changed else "  trigger=WITH_TRACK"))
                            # If phrase count changed vs what plan was built on,
                            # the plan indices are wrong — invalidate and re-request.
                            # Also handles the 0→real-phrases case where plan guard
                            # is still blocking a re-plan.
                            if self._ai_control and not _tid_changed:
                                _plan_ph = self._ai_control._track_phrases.get(_tid)
                                _live_n  = len(_p_snap.phrases)
                                _plan_n  = len(_plan_ph) if _plan_ph else 0
                                if _plan_n == 0 or abs(_live_n - _plan_n) > 1:
                                    if _plan_n == 0:
                                        self._tlog_write("PHRASES_ARRIVED_REPLAN",
                                            f"deck=P{_p_num}  track_id={_tid}  "
                                            f"live_phrases={_live_n}  prev_plan_phrases=0  "
                                            f"clearing_plan_guard")
                                    else:
                                        self._tlog_write("PHRASES_CHANGED",
                                            f"deck=P{_p_num}  track_id={_tid}  "
                                            f"plan_phrases={_plan_n}  live_phrases={_live_n}  "
                                            f"invalidating_plan")
                                    self._ai_control._track_plans.pop(_tid, None)
                                    self._ai_control._track_phrases.pop(_tid, None)
                                    self._ai_plan_tids.pop(_p_num, None)
                        elif _analysis_arrived:
                            # Mark as handled so we don't re-trigger every frame
                            self._ai_phrases_have[_p_num] = _tid
                            self._ai_phrases_logged.pop(_p_num, None)
                            self._ai_phrases_pending.pop(_p_num, None)
                            self._tlog_write("ANALYSIS_ARRIVED",
                                f"deck=P{_p_num}  track_id={_tid}  (waveform-only mode)")

                        if _tid_changed:
                            # New track — cancel old plan, reset phrase tracking, clear stale state
                            if _prev_tid:
                                self._ai_control._planning_tids.discard(_prev_tid)
                                _still_used = any(
                                    self._blt.get_snapshot(p).track_id == _prev_tid
                                    for p in range(1, 4) if p != _p_num
                                )
                                if not _still_used:
                                    self._ai_control._track_plans.pop(_prev_tid, None)
                                    self._ai_control._plan_fail_times.pop(_prev_tid, None)
                            self._ai_plan_tids.pop(_p_num, None)
                            self._ai_plan_applied.clear()
                            self._blt_phrase_idx_last = -1
                            self._ai_phrases_have.pop(_p_num, None)
                            self._ai_phrases_pending[_p_num] = (_tid, _now)

                        _trigger = ("TID" if _tid_changed else
                                    "URL" if _url_changed else "PHRASES")
                        self._tlog_write("NOTIFY_TRACK",
                            f"deck=P{_p_num}  track_id={_tid}  trigger={_trigger}"
                            f"  has_phrases={_has_phrases}"
                            f"  url={'YES' if _url else 'NO'}"
                            f"  title={getattr(_p_snap, 'title', '')!r:.30}")
                        self._ai_control.notify_track_loaded(
                            _url, _tid,
                            getattr(_p_snap, "title", ""),
                            getattr(_p_snap, "artist", ""),
                            getattr(_p_snap, "duration_ms", 0),
                            player_num=_p_num)
                    except Exception:
                        pass

        try:
            self._render_led_canvas(px_devs, fallback_px=px)
        except Exception as e:
            _log.debug("Canvas render error: %s", e)

        try:
            self._effects_panel.update_strips(px)
        except Exception as e:
            _log.debug("Strip update error: %s", e)

        # Keep effect badge green on whichever effect is playing.
        # Priority: manually selected choreo-mode button > plan effect name > active instance.
        if self._active_choreo_mode_cls is not None:
            _play_cls = self._active_choreo_mode_cls
        else:
            _cm_eff = getattr(self._choreo_mgr, "_current_effect_name", "") if self._choreo_mgr else ""
            if _cm_eff:
                _play_cls = self._effects_panel.get_by_name(_cm_eff)
            else:
                inst = self._active_inst or self._blt_phrase_inst
                _play_cls = type(inst) if inst else None
        self._effects_panel.set_playing(_play_cls)

        # Bridge health check — auto-restart if clients exist but no packets for 10s
        if PRODJ_OK and self._prodj and not self._bridge_restarting:
            try:
                clients = self._prodj.cl.clients
                if clients:
                    stale = time.time() - max((c.ttl for c in clients), default=time.time())
                    if stale > 25.0:
                        _log.warning("No ProDJ packets for %.0fs — auto-restarting bridge", stale)
                        threading.Thread(target=self._restart_bridge, daemon=True).start()
            except Exception:
                pass

        try:
            # Always advance hysteresis so render thread gets correct player
            if BLT_OK:
                _prev_committed = self._blt_committed_player
                self._blt_committed_player = self._cdj_tab.tick_selection(
                    self._blt, self._prodj if PRODJ_OK else None)
                if self._blt_committed_player != _prev_committed and self._blt_committed_player:
                    _trigger = self._cdj_tab._last_trigger
                    self._tlog_write("PLAYER_COMMITTED",
                        f"P{_prev_committed or '?'}→P{self._blt_committed_player}  trigger={_trigger}")
                    self._add_deck_event(self._blt_committed_player, "committed",
                                         f"trigger={_trigger}  prev=P{_prev_committed or '?'}")
                    if self._ai_control:
                        self._ai_control.set_committed_player(self._blt_committed_player)
            if self._tabview.get() == "CDJ MONITOR":
                self._cdj_tab.update(self._prodj, self._blt)
        except Exception as e:
            _log.debug("CDJ tab update error: %s", e)

        self._update_status_bar()

    def _sb_set_pin(self, val):
        """Pin deck from status bar — mirrors CDJ tab pin buttons."""
        self._cdj_tab._set_led_pin(val)
        # Sync status bar buttons
        for v, b in self._sb_pin_btns.items():
            active = (v == val)
            b.configure(fg_color=ACCENT if active else BG3,
                        text_color="#000000" if active else DIM)

    def _update_status_bar(self):
        try:
            # Deck pin button highlight — only redraw when pin value changes
            pinned = getattr(self._cdj_tab, "_led_pin", 0)
            if pinned != self._sb_last_pin:
                self._sb_last_pin = pinned
                for v, b in self._sb_pin_btns.items():
                    active = (v == pinned)
                    b.configure(fg_color=ACCENT if active else BG3,
                                text_color="#000000" if active else DIM)

            # Deck
            pnum = self._blt_committed_player or 0
            deck_key = (pnum,)
            if deck_key != self._sb_last_deck:
                self._sb_last_deck = deck_key
                self._sb_deck.configure(
                    text=f"LED: P{pnum}" if pnum else "LED: —",
                    text_color=ACCENT if pnum else DIM)

            # Track title + artist — use render-thread cache, never call _best_blt_snap() here
            snap = self._sb_snap_cache
            if snap and (snap.has_track or snap.is_playing):
                title  = (snap.title  or "")[:28]
                artist = (snap.artist or "")[:20]
                track_str = f"{title}"
                if artist:
                    track_str += f"  –  {artist}"
                if track_str != self._sb_last_track:
                    self._sb_last_track = track_str
                    self._sb_track.configure(text=track_str, text_color=TEXT)

                phrase_str = (snap.current_phrase.kind if snap.current_phrase else "—") or "—"
                if phrase_str != self._sb_last_phrase:
                    self._sb_last_phrase = phrase_str
                    self._sb_phrase.configure(text=phrase_str, text_color=ACCENT2)
            else:
                if self._sb_last_track != "":
                    self._sb_last_track  = ""
                    self._sb_last_phrase = ""
                    self._sb_track.configure(text="no track", text_color=DIM)
                    self._sb_phrase.configure(text="—", text_color=DIM)

            # AI vs RANDOM mode — in choreo/demo mode show the engine mode
            _ai_has_plan = (self._ai_control is not None
                            and self._ai_control._ai_ready
                            and self._blt_committed_player is not None
                            and self._sb_snap_cache is not None
                            and self._sb_snap_cache.track_id in self._ai_control._track_plans)
            _choreo_mode = getattr(self._choreo_mgr, "_mode", "") if self._choreo_mgr else ""
            mode_key = (_ai_has_plan, _choreo_mode)
            if mode_key != self._sb_last_mode:
                self._sb_last_mode = mode_key
                if _choreo_mode:
                    _MODE_ABBR_SB = {
                        "noise": "NOISE", "interstitial_chase": "CHASE",
                        "vertical_pulse": "PULSE", "mirror_symmetry": "SYMM",
                        "digital_rain": "RAIN", "diamond_wave": "DWAVE",
                        "diamond_chase": "DCHSE", "diamond_bloom": "DBLOM",
                        "minimal_ambient": "MAMB", "ember_drift": "EMBR",
                        "deep_void": "VOID",
                    }
                    _mabbr = _MODE_ABBR_SB.get(_choreo_mode, _choreo_mode[:6].upper())
                    self._sb_mode.configure(text=_mabbr, text_color="#aaffcc")
                elif _ai_has_plan:
                    self._sb_mode.configure(text="AI", text_color="#00ff88")
                else:
                    self._sb_mode.configure(text="RANDOM", text_color=ACCENT)

            # Effect name — plan effect name takes priority in choreo/demo mode
            _cm_eff_sb = getattr(self._choreo_mgr, "_current_effect_name", "") if self._choreo_mgr else ""
            if _cm_eff_sb:
                ename = _cm_eff_sb
                _ename_col = "#00ff88"  # green = plan-driven
            else:
                inst  = self._active_inst or self._blt_phrase_inst
                ename = type(inst).__name__ if inst else "—"
                _ename_col = TEXT if inst else DIM
            if ename != self._sb_last_effect:
                self._sb_last_effect = ename
                self._sb_effect.configure(text=ename, text_color=_ename_col)

            # Palette name — always reflects what the render thread is actually using
            _pname = self._palette_mgr.active.name
            if _pname != self._sb_last_palette:
                self._sb_last_palette = _pname
                self._sb_palette_lbl.configure(text=_pname, text_color=ACCENT2)

            # Live effect parameters — choreo strip state OR AI effect params
            _cm = self._choreo_mgr
            if _cm is not None:
                # ChoreographyManager is active — show live per-strip diversity
                _MODE_ABBR = {
                    "noise":              "NOISE",
                    "interstitial_chase": "CHASE",
                    "vertical_pulse":     "PULSE",
                    "mirror_symmetry":    "SYMM",
                    "digital_rain":       "RAIN",
                    "diamond_wave":       "DWAVE",
                    "diamond_chase":      "DCHSE",
                    "diamond_bloom":      "DBLOM",
                    "minimal_ambient":    "MAMB",
                    "ember_drift":        "EMBR",
                    "deep_void":          "VOID",
                }
                _mode_s = _MODE_ABBR.get(_cm._mode or "noise", (_cm._mode or "?")[:5].upper())
                _act_s = "".join(str(s + 1) for s in sorted(_cm._active_strips))
                # Palette from plan (updated by _apply_plan_entry)
                _pal_s  = getattr(_cm, "_current_palette", "—")[:8]
                # Direction / grouping / spacing from plan
                _dir_s  = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(
                    getattr(_cm, "_current_direction", "forward"), "FWD")
                _grp_s  = str(getattr(_cm, "_current_grouping", 1))
                _spc_s  = str(getattr(_cm, "_current_spacing",  0))
                # Per-strip speeds: 5 single-decimal values
                _spd_s = "·".join(f"{_cm._strip_speeds[s].cur:.1f}" for s in range(5))
                # Per-strip offsets: compact signed ints
                _off_s = "·".join(f"{int(round(_cm._strip_offsets[s].cur)):+d}" for s in range(5))
                # Reverse mask: strip numbers (1-indexed) that run backwards
                _rev_s = "".join(str(s + 1) for s in range(5) if _cm._strip_reverse[s])
                # Mirror: "1←5 2←4"
                _mir_s = " ".join(
                    f"{s+1}←{_cm._strip_mirror[s]+1}"
                    for s in range(5) if _cm._strip_mirror[s] is not None
                )
                _params_str = (
                    f"{_mode_s} [{_act_s}]  {_pal_s}  {_dir_s}  G{_grp_s} S{_spc_s}  "
                    f"v {_spd_s}  o {_off_s}"
                    + (f"  R{_rev_s}" if _rev_s else "")
                    + (f"  M {_mir_s}" if _mir_s else "")
                )
            else:
                # WLED effects path — show AI effect params
                _sp   = self._speed_var.get()
                _grp  = getattr(self, "_ai_grouping", 1)
                _spc  = getattr(self, "_ai_spacing",  0)
                _off  = getattr(self, "_ai_offset",   0.0)
                _dir  = getattr(self, "_ai_direction", "forward")
                _astr = getattr(self, "_ai_active_strips", [1,2,3,4,5])
                _dir_s = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(_dir, _dir[:3].upper())
                _strips_s = "".join(str(s) for s in sorted(_astr))
                _params_str = (f"SP {_sp:.2f}  G {_grp}  S {_spc}  "
                               f"O {_off:.2f}  {_dir_s}  [{_strips_s}]")
            if _params_str != self._sb_last_params:
                self._sb_last_params = _params_str
                self._sb_params.configure(text=_params_str, text_color=TEXT)

            # ── Sync bottom-bar controls to choreo plan state ──────────────
            # When choreo/demo is active, the GROUP, SPACE, DIRECTION buttons
            # and OFFSET/SPD-MUL labels should reflect what the plan chose.
            _cm2 = self._choreo_mgr
            if _cm2 is not None:
                # GROUP segmented button — skip if user manually overrode this phrase
                if not self._manual_grouping:
                    _cg = getattr(_cm2, "_current_grouping", 1)
                    _nearest_grp = min(["1","2","3","5","8"], key=lambda v: abs(int(v) - _cg))
                    _gseg = getattr(self, "_grouping_seg", None)
                    if _gseg:
                        try:
                            if _gseg.get() != _nearest_grp:
                                _gseg.set(_nearest_grp)
                        except Exception: pass

                # SPACE segmented button — skip if user manually overrode this phrase
                if not self._manual_spacing:
                    _cs = getattr(_cm2, "_current_spacing", 0)
                    _nearest_spc = min(["0","2","4","8","16"], key=lambda v: abs(int(v) - _cs))
                    _sseg = getattr(self, "_spacing_seg", None)
                    if _sseg:
                        try:
                            if _sseg.get() != _nearest_spc:
                                _sseg.set(_nearest_spc)
                        except Exception: pass

                # DIRECTION segmented button
                _cd = getattr(_cm2, "_current_direction", "forward")
                _dir_btn = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(_cd, "FWD")
                _dseg = getattr(self, "_direction_seg", None)
                if _dseg:
                    try:
                        if _dseg.get() != _dir_btn:
                            _dseg.set(_dir_btn)
                    except Exception: pass

                # OFFSET label — compact per-strip signed ints
                _off_str = "·".join(
                    f"{int(round(_cm2._strip_offsets[s].cur)):+d}" for s in range(5))
                _off_lbl = getattr(self, "_offset_label", None)
                if _off_lbl and _off_str != self._offset_label_last:
                    self._offset_label_last = _off_str
                    try: _off_lbl.configure(text=_off_str)
                    except Exception: pass

                # SPD MUL label — per-strip speed multipliers
                _spd_str = "·".join(f"{_cm2._strip_speeds[s].cur:.2f}" for s in range(5))
                _spd_lbl = getattr(self, "_spdmul_label", None)
                if _spd_lbl and _spd_str != self._spdmul_label_last:
                    self._spdmul_label_last = _spd_str
                    try: _spd_lbl.configure(text=_spd_str)
                    except Exception: pass

            # WLED device connection status (auto-ping every 10s)
            _devs = self._device_mgr.enabled_devices()
            if _devs:
                _now_p = time.time()
                if _now_p - self._last_ping_t >= 10.0:
                    self._last_ping_t = _now_p
                    for _pd in _devs:
                        self._device_mgr.ping(_pd,
                            callback=lambda _d: self.after(0, self._update_wled_dot))
            else:
                if self._sb_wled_last is not None:
                    self._sb_wled_last = None
                    self._sb_wled.configure(text="● WLED", text_color="gray")

            # Feed mobile feedback server
            if self._feedback:
                _fb_snap = self._sb_snap_cache
                _fb_pal  = self._palette_mgr.palettes[self._palette_mgr._active].name
                self._feedback.update_state(
                    track   = getattr(_fb_snap, "title",  "") if _fb_snap else "",
                    artist  = getattr(_fb_snap, "artist", "") if _fb_snap else "",
                    phrase  = self._blt_phrase_last or "",
                    effect  = ename,
                    palette = _fb_pal,
                    bpm     = self._bpm_var.get(),
                    energy  = "stable",
                )
        except Exception:
            pass

    def _render_led_canvas(self, px_devs: list, fallback_px=None):
        w, h = self._canvas_w, self._canvas_h
        if w < 4 or h < 4:
            return

        img = np.full((h, w, 3), 10, dtype=np.uint8)

        # Build lookup: dev ip → px array
        dev_px_map = {dev.ip: dpx for dev, dpx in px_devs}

        # Demo mode always uses the virtual 5-strip renderer so per-strip
        # activity (active_strips, mode differences) is clearly visible
        # regardless of the configured device geometry.
        use_virtual = (not self._led_bands) or self._demo_mode
        if use_virtual and fallback_px is not None and len(fallback_px) >= 300:
            # ── Virtual 5-strip snake preview (no real devices configured) ──
            n_strips = 5
            lps      = len(fallback_px) // n_strips
            strip_h  = max(5, min(14, (h - 30) // (n_strips * 2)))
            gap_h    = max(5, strip_h // 2 + 2)
            total_h  = n_strips * strip_h + (n_strips - 1) * gap_h
            start_y  = max(4, (h - total_h) // 2)
            for s in range(n_strips):
                sy     = start_y + s * (strip_h + gap_h)
                lo, hi = s * lps, min(len(fallback_px), (s + 1) * lps)
                seg_px = fallback_px[lo:hi]
                if s % 2 == 1:          # snake reversal on even strips
                    seg_px = seg_px[::-1]
                row = _process_strip_preview(seg_px, w)
                for dy in range(strip_h):
                    ry = sy + dy
                    if 0 <= ry < h:
                        img[ry] = row

            # ── Back strip preview row ──────────────────────────────────────
            _back_px = getattr(self, "_last_back_px", None)
            if _back_px is not None and len(_back_px) > 0:
                _bs_h = max(4, min(8, strip_h - 2))
                _bs_y = start_y + n_strips * (strip_h + gap_h) + 4
                _bs_row = _process_strip_preview(_back_px, w)
                for _dy in range(_bs_h):
                    _ry = _bs_y + _dy
                    if 0 <= _ry < h:
                        img[_ry] = _bs_row

            photo = ImageTk.PhotoImage(Image.fromarray(img))
            self._led_canvas.itemconfig(self._canvas_img_id, image=photo)
            self._canvas_photo = photo

            # Overlay: phrase + mode label
            self._led_canvas.delete("devlabel")
            if self._choreo_mgr is not None:
                mgr = self._choreo_mgr
                phrase = mgr._current_phrase or "—"
                mode   = mgr._mode or "noise"
                pal    = getattr(mgr, "_current_palette",   "—")
                dirn   = getattr(mgr, "_current_direction", "—")
                grp    = getattr(mgr, "_current_grouping",  1)
                spc    = getattr(mgr, "_current_spacing",   0)
                act_s  = ",".join(str(s + 1) for s in mgr._active_strips)
                dir_abbr = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(dirn, dirn[:3].upper())
                # Top-center line 1: phrase + mode
                self._led_canvas.create_text(
                    w // 2, 6,
                    text=f"{phrase.upper()}  ·  {mode.replace('_', ' ')}",
                    anchor="n", fill="#ccffcc",
                    font=("Helvetica Neue", 10, "bold"), tags="devlabel")
                # Top-center line 2: palette / direction / grouping / strips
                self._led_canvas.create_text(
                    w // 2, 22,
                    text=f"{pal}  {dir_abbr}  G{grp} S{spc}  [{act_s}]",
                    anchor="n", fill="#aaffcc",
                    font=("Helvetica Neue", 8), tags="devlabel")
                # Bottom-left: full state_summary in small text
                self._led_canvas.create_text(
                    6, h - 4,
                    text=mgr.state_summary(),
                    anchor="sw", fill="#3a7a3a",
                    font=("Helvetica Neue", 7), tags="devlabel")
            # Back strip label
            if _back_px is not None and len(_back_px) > 0:
                self._led_canvas.create_text(
                    6, _bs_y - 2,
                    text="BACK 768",
                    anchor="sw", fill="#557755",
                    font=("Helvetica Neue", 7), tags="devlabel")
            return

        if self._led_bands:
            for dev, led_map, y0, bh in self._led_bands:
                n   = dev.led_count
                dpx = dev_px_map.get(dev.ip)
                if dpx is None:
                    dpx = np.zeros((n, 3), dtype=np.uint8)

                geom = Geometry.from_dict(dev.geometry)

                if isinstance(geom, SnakeGeometry):
                    # ── Thin horizontal strips with physical spacing ───────────
                    # Physical: 5m strips, 30cm gaps → gap = 6% of strip length
                    # Visual: strip_h thin, gap_h proportional
                    S        = geom.segments
                    N        = geom.leds_per_segment
                    strip_h  = max(4, min(8, (bh - 20) // max(1, S * 5)))
                    margin   = 6
                    # gap proportional to 30cm / 500cm = 0.06 of horizontal run
                    gap_ratio = 0.30 / 5.0                          # physical ratio
                    gap_h     = max(6, int(w * gap_ratio * 0.36))   # visual gap px
                    total_h   = S * strip_h + (S - 1) * gap_h
                    # Centre block vertically in the band
                    start_y   = y0 + max(margin, (bh - total_h) // 2)

                    for seg in range(S):
                        sy = start_y + seg * (strip_h + gap_h)
                        if sy + strip_h > y0 + bh:
                            break
                        seg_px = dpx[seg * N : (seg + 1) * N]
                        if seg % 2 == 1:            # snake reversal
                            seg_px = seg_px[::-1]
                        row = _process_strip_preview(seg_px, w)     # (w, 3)
                        for dy in range(strip_h):
                            ry = sy + dy
                            if 0 <= ry < h:
                                img[ry] = row

                elif isinstance(geom, LinearGeometry):
                    # Single 1-D strip: apply the full post-processing pipeline
                    # then stamp it into the rows that the led_map marks as valid.
                    processed = _process_strip_preview(dpx, w)   # (w, 3)
                    valid_rows = np.where((led_map >= 0).any(axis=1))[0]
                    for ry in valid_rows:
                        abs_ry = y0 + ry
                        if 0 <= abs_ry < h:
                            img[abs_ry] = processed

                else:
                    # GlobeGeometry (2-D Voronoi map): sample colours from the
                    # processed pixel array so gamma + bloom still apply.
                    band  = img[y0:y0 + bh]
                    valid = (led_map >= 0) & (led_map < n)
                    if valid.any():
                        # Build a processed lookup for every LED index
                        proc  = _process_strip_preview(dpx, n)   # (n, 3)  1-D pass
                        band[valid] = proc[led_map[valid]]
                        # 2-D diffusion + bloom on the stamped band
                        band_f = band.astype(np.float32)
                        for ch in range(3):
                            band_f[:, :, ch] = _scipy_gf2d(band_f[:, :, ch], sigma=1.2)
                        lum = band_f.max(axis=2)
                        bright = lum > 150.0
                        if bright.any():
                            glow_src = np.where(bright[:, :, np.newaxis], band_f, 0.0)
                            for ch in range(3):
                                glow_src[:, :, ch] = _scipy_gf2d(glow_src[:, :, ch], sigma=3.0)
                            band_f = band_f + glow_src * 0.55
                        img[y0:y0 + bh] = band_f.clip(0, 255).astype(np.uint8)

        # ── Back strip preview row (real-device path) ──────────────────────
        _back_px_r = getattr(self, "_last_back_px", None)
        if _back_px_r is not None and len(_back_px_r) > 0:
            # Place below all device bands
            _bs_y_r = max((y0 + bhi for _, _, y0, bhi in self._led_bands), default=0) + 6
            _bs_h_r = 6
            _bs_row_r = _process_strip_preview(_back_px_r, w)
            for _dy in range(_bs_h_r):
                _ry = _bs_y_r + _dy
                if 0 <= _ry < h:
                    img[_ry] = _bs_row_r

        photo = ImageTk.PhotoImage(Image.fromarray(img))
        self._led_canvas.itemconfig(self._canvas_img_id, image=photo)
        self._canvas_photo = photo

        # Device + phrase/mode labels
        self._led_canvas.delete("devlabel")
        for dev, _, y0, _ in self._led_bands:
            self._led_canvas.create_text(
                6, y0 + 4,
                text=f"{dev.name}  {dev.ip}  ({dev.led_count} LEDs  {dev.geometry.get('type','?')})",
                anchor="nw", fill="#1a3a1a",
                font=("Helvetica Neue", 8), tags="devlabel")
        if self._choreo_mgr is not None:
            mgr = self._choreo_mgr
            phrase = mgr._current_phrase or "—"
            mode   = mgr._mode or "noise"
            pal    = getattr(mgr, "_current_palette",   "—")
            dirn   = getattr(mgr, "_current_direction", "—")
            grp    = getattr(mgr, "_current_grouping",  1)
            spc    = getattr(mgr, "_current_spacing",   0)
            act_s  = ",".join(str(s + 1) for s in mgr._active_strips)
            dir_abbr = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(dirn, dirn[:3].upper())
            self._led_canvas.create_text(
                w // 2, 4,
                text=f"{phrase.upper()}  ·  {mode.replace('_', ' ')}",
                anchor="n", fill="#ccffcc",
                font=("Helvetica Neue", 10, "bold"), tags="devlabel")
            self._led_canvas.create_text(
                w // 2, 20,
                text=f"{pal}  {dir_abbr}  G{grp} S{spc}  [{act_s}]",
                anchor="n", fill="#aaffcc",
                font=("Helvetica Neue", 8), tags="devlabel")
            self._led_canvas.create_text(
                6, h - 4,
                text=mgr.state_summary(),
                anchor="sw", fill="#3a7a3a",
                font=("Helvetica Neue", 7), tags="devlabel")
        if _back_px_r is not None and len(_back_px_r) > 0:
            self._led_canvas.create_text(
                6, _bs_y_r - 2,
                text="BACK 768",
                anchor="sw", fill="#557755",
                font=("Helvetica Neue", 7), tags="devlabel")

    def _update_wled_dot(self):
        """Refresh WLED status dot in status bar. Called on main thread after ping."""
        devs = self._device_mgr.enabled_devices()
        if not devs:
            online = None
        elif all(d.online is True for d in devs):
            online = True
        elif any(d.online is False for d in devs):
            online = False
        else:
            online = None  # still unknown
        if online == self._sb_wled_last:
            return
        self._sb_wled_last = online
        if online is True:
            self._sb_wled.configure(text="● WLED", text_color="#00cc44")
        elif online is False:
            self._sb_wled.configure(text="○ WLED", text_color="#cc2222")
        else:
            self._sb_wled.configure(text="● WLED", text_color="gray")

    # ── ProDJ restart / health ────────────────────────────────────────────────

    def _restart_bridge(self):
        """Stop the current ProDJ bridge, clear all caches, and start fresh."""
        if not PRODJ_OK or self._bridge_restarting:
            return
        self._bridge_restarting = True
        _log.info("Restarting ProDJ bridge…")
        old = self._prodj
        self._prodj = None               # UI immediately shows "connecting…"
        self._cdj_last_tid.clear()       # allow _on_cdj_change to re-request for all tracks
        self._cdj_tab._retry_times.clear()
        if old:
            try:
                old.stop()
            except Exception as e:
                _log.debug("prodj.stop() error during restart: %s", e)
        self._start_prodj()              # creates new ProDj with fresh DataStores
        self._bridge_restarting = False
        _log.info("ProDJ bridge restarted")

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self):
        self._running = False
        # Quit BLT
        try:
            import subprocess as _sp
            _sp.Popen(["osascript", "-e", 'tell application "Beat Link Trigger" to quit'],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        self._save_prefs()
        self._save_session_memory()
        try:
            self._tlog_write("SESSION_END", f"uptime={(time.perf_counter()-self._tlog_t0):.1f}s  "
                             f"history_saved={len(self._phrase_history)}")
            self._tlog.close()
        except Exception:
            pass
        for d in self._device_mgr.enabled_devices():
            self._sender.send(np.zeros((d.led_count, 3), dtype=np.uint8), d.ip)
        self._sender.close()
        if self._prodj:
            try: self._prodj.stop()
            except: pass
        self.destroy()
        # Close the Terminal window that launched this process
        try:
            import subprocess
            tty = None
            for fd in (0, 1, 2):
                try:
                    tty = os.ttyname(fd)
                    break
                except OSError:
                    pass
            if tty:
                subprocess.Popen(["osascript", "-e", f'''
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t is equal to "{tty}" then
                close w saving no
                exit repeat
            end if
        end repeat
    end repeat
end tell
'''], start_new_session=True)
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    # ── macOS system priority — start before UI so App Nap is off from tick 0 ──
    try:
        from system_priority import SystemPriority
        import sys as _sys
        _sys.modules["__main__"]._system_priority = SystemPriority()
        _sys.modules["__main__"]._system_priority.start()
    except Exception as _sp_err:
        print(f"[SystemPriority] kon niet starten: {_sp_err}")

    app = DJLightsApp()
    app.mainloop()
