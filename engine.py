"""
djlights — Lighting Engine

Data sources (all from direct Pro DJ Link via prodj library):
  player.bpm              — current BPM
  player.beat             — beat number within bar (1-4)
  player.position         — track position in seconds (dead-reckoned from beatgrid)
  player.key              — musical key (int from status packet)
  player.play_state       — playing / paused / cued / etc.
  player.on_air           — mixer channel live

  prodj.data.color_waveform_store  — color scroll waveform (PWV5)
    each entry: 16-bit int, bits: [15:13]=red(0-7) [12:10]=green(0-7) [9:7]=blue(0-7) [6:2]=height
    ~150 segments/second

  prodj.data.pssi_store    — phrase analysis
    entries: [{beat, kind_name, fill, beat_fill}, ...]
    kind_name: intro / verse / up / down / chorus / bridge / outro

  prodj.data.cues (PCO2)   — hot cues with RGB colors, parsed via parse_cue_extended_entries()
"""
import colorsys
import math
import random
import time
import logging

import numpy as np

import wled

log = logging.getLogger("engine")

LED_COUNT = wled.LED_COUNT
WAVEFORM_FPS = 150   # PWV5 color scroll waveform segments per second

# ── Key → hue (Camelot wheel mapping) ────────────────────────────────────────
# CDJ key values: 0=unknown, 1-24 match Camelot positions
_KEY_HUE = {
    1:0, 2:20, 3:30, 4:50, 5:60, 6:80, 7:90, 8:110, 9:120, 10:140,
    11:150, 12:170, 13:180, 14:200, 15:210, 16:230, 17:240, 18:260,
    19:270, 20:290, 21:300, 22:320, 23:330, 24:350,
}

# ── Phrase → base hue ─────────────────────────────────────────────────────────
_PHRASE_HUE = {
    "intro":  240,
    "verse":  120,
    "up":     40,
    "chorus": 0,
    "bridge": 280,
    "outro":  220,
    "down":   200,
}

# ── Beat pulse ────────────────────────────────────────────────────────────────
_beat_pulse_at    = 0.0
_beat_pulse_bri   = 0.0
BEAT_PULSE_DECAY  = 0.18   # seconds

# ── Hot cue flash ─────────────────────────────────────────────────────────────
_hotcue_flash_at       = 0.0
_hotcue_flash_color    = (255, 255, 255)
HOTCUE_FLASH_DURATION  = 0.12

# ── Phrase → effect pools (random pick on phrase change) ─────────────────────
_PHRASE_EFFECTS = {
    "intro":  ["breathe", "gradient"],
    "verse":  ["gradient", "chase_slow"],
    "up":     ["chase", "sparkle", "gradient"],
    "chorus": ["pulse_chase", "chase", "sparkle"],
    "bridge": ["ripple", "gradient"],
    "down":   ["breathe", "gradient"],
    "outro":  ["breathe"],
    "":       ["gradient"],
}

# ── Last known phrase state ───────────────────────────────────────────────────
_current_phrase   = ""
_active_effect    = "gradient"   # chosen at phrase change
_phrase_beat      = 0    # beat within current phrase
_last_beat_n      = 0    # for detecting new beats

# ── Current hot cues per player ───────────────────────────────────────────────
_hot_cues: dict[int, list] = {}   # player_number → list of cue dicts
_last_cue_pos: dict[int, float] = {}


def on_status(player) -> None:
    """
    Called each render cycle with the current player client object.
    Updates phrase and beat state.
    """
    global _current_phrase, _active_effect, _phrase_beat, _last_beat_n
    global _beat_pulse_at, _beat_pulse_bri

    beat_n = player.beat  # beat within bar 1-4
    if beat_n != _last_beat_n and beat_n > 0:
        _last_beat_n    = beat_n
        _beat_pulse_at  = time.time()
        _beat_pulse_bri = 110 if beat_n == 1 else 45

    # Detect phrase change → pick a new random effect from that phrase's pool
    phrase = _current_phrase_for(player)
    if phrase != _current_phrase:
        _current_phrase = phrase
        pool = _PHRASE_EFFECTS.get(phrase, _PHRASE_EFFECTS[""])
        _active_effect  = random.choice(pool)
        log.info("Phrase → %r  effect → %r", phrase, _active_effect)


def set_pssi(player_number: int, player, pssi: dict, beatgrid) -> None:
    """
    Called when PSSI phrase data arrives for a track.
    pssi: {mood, end_beat, entries:[{beat, kind_name, fill, beat_fill}]}
    beatgrid: list of {beat, time} dicts
    Stored as attribute on player for use in render_frame().
    """
    player._pssi     = pssi
    player._beatgrid = beatgrid
    log.info("P%d PSSI loaded: mood=%d %d phrases", player_number, pssi.get("mood", 0), len(pssi.get("entries", [])))


def set_hot_cues(player_number: int, cues: list) -> None:
    """Store parsed extended cue entries for a player."""
    _hot_cues[player_number] = cues
    log.info("P%d hot cues: %d", player_number, len(cues))


def _current_phrase_for(player) -> str:
    pssi = getattr(player, "_pssi", None)
    if not pssi:
        return ""
    beat_n = player.beat_count  # absolute beat in track (from beatgrid integration)
    if beat_n is None or beat_n <= 0:
        return ""
    entries = pssi.get("entries", [])
    phrase = ""
    for e in entries:
        if e["beat"] <= beat_n:
            phrase = e["kind_name"]
        else:
            break
    return phrase


def _waveform_color_at(player, color_waveform) -> tuple[int,int,int] | None:
    """
    Extract RGB color from PWV5 color scroll waveform at current position.
    Returns (r, g, b) each 0-255, or None if no waveform.
    """
    if not color_waveform:
        return None
    pos_s = player.position if player.position is not None else 0.0
    idx = int(pos_s * WAVEFORM_FPS)
    if idx < 0 or idx >= len(color_waveform):
        return None
    v   = color_waveform[idx]
    r   = ((v >> 13) & 0x07) * 255 // 7
    g   = ((v >> 10) & 0x07) * 255 // 7
    b   = ((v >>  7) & 0x07) * 255 // 7
    return (r, g, b)


def _check_hotcue_hit(player_number: int, player) -> None:
    global _hotcue_flash_at, _hotcue_flash_color

    cues = _hot_cues.get(player_number, [])
    if not cues:
        return
    pos_ms = (player.position or 0.0) * 1000.0
    bpm    = player.bpm or 120
    beat_dur_ms = 60000.0 / bpm

    prev = _last_cue_pos.get(player_number, -999999)
    for cue in cues:
        if cue.get("hot_cue", 0) == 0:
            continue   # memory cue, skip
        cue_ms = cue["time_ms"]
        if abs(cue_ms - pos_ms) < beat_dur_ms * 0.5:
            if abs(cue_ms - prev) > beat_dur_ms:
                _hotcue_flash_at    = time.time()
                _hotcue_flash_color = (cue.get("color_r", 255),
                                       cue.get("color_g", 255),
                                       cue.get("color_b", 255))
                _last_cue_pos[player_number] = cue_ms
                break


def _hsv(h_deg: float, s: float, v: float) -> tuple[int,int,int]:
    r, g, b = colorsys.hsv_to_rgb(h_deg / 360.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def render_frame(player, player_number: int, prodj_data) -> np.ndarray:
    """
    Render a full LED frame from current Pro DJ Link state.

    player      — ProDj client object (prodj.core.clientlist client)
    player_number — int
    prodj_data  — prodj.data.DataProvider instance
    """
    now = time.time()

    if player is None or player.play_state not in ("playing", "cueing"):
        return wled.black()

    # ── Check for hot cue hit ─────────────────────────────────────────────────
    _check_hotcue_hit(player_number, player)

    # ── Waveform color at current position ────────────────────────────────────
    key = (player.loaded_player_number, player.loaded_slot, player.track_id)
    color_waveform = prodj_data.color_waveform_store.get(key) if prodj_data else None
    wv_rgb = _waveform_color_at(player, color_waveform)

    # ── Key-based hue (BLT key is string e.g. "6A"; skip numeric lookup) ────────
    key_hue = None
    if player.key and isinstance(player.key, int):
        key_hue = _KEY_HUE.get(player.key)

    # ── Phrase (already tracked in on_status; use cached value) ───────────────
    phrase     = _current_phrase
    phrase_hue = _PHRASE_HUE.get(phrase, 180)

    # ── Final hue: phrase + waveform color blend ──────────────────────────────
    hue = phrase_hue
    if wv_rgb is not None:
        wvr, wvg, wvb = [x / 255.0 for x in wv_rgb]
        wv_h, wv_s, _  = colorsys.rgb_to_hsv(wvr, wvg, wvb)
        if wv_s > 0.25:
            hue = (hue * 0.7 + wv_h * 360 * 0.3) % 360

    # ── Beat pulse ────────────────────────────────────────────────────────────
    beat_age = now - _beat_pulse_at
    pulse    = max(0.0, 1.0 - beat_age / BEAT_PULSE_DECAY) * (_beat_pulse_bri / 255.0)

    # ── Base brightness by phrase ─────────────────────────────────────────────
    base_bri = {
        "intro": 0.45, "verse": 0.55, "up": 0.65,
        "chorus": 0.85, "bridge": 0.60, "outro": 0.35, "down": 0.40, "": 0.5,
    }.get(phrase, 0.5)
    bri = min(1.0, base_bri + pulse)

    # ── Run the active effect (chosen at phrase change) ───────────────────────
    pixels = _effect(player, _active_effect, hue, bri, now)

    # ── Hot cue flash overlay ─────────────────────────────────────────────────
    flash_age = now - _hotcue_flash_at
    if flash_age < HOTCUE_FLASH_DURATION:
        alpha  = (1.0 - flash_age / HOTCUE_FLASH_DURATION) ** 2
        fc     = _hotcue_flash_color
        flash  = np.array([[int(fc[0]*alpha), int(fc[1]*alpha), int(fc[2]*alpha)]], dtype=np.int32)
        pixels = np.clip(pixels.astype(np.int32) + flash, 0, 255).astype(np.uint8)

    return pixels


def _effect(player, effect: str, hue: float, bri: float, now: float) -> np.ndarray:
    bpm = player.bpm if player.bpm and player.bpm > 0 else 120.0

    if effect == "pulse_chase":
        return _fx_pulse_chase(hue, bri, bpm, now)
    elif effect == "chase":
        return _fx_chase(hue, bri, bpm, now)
    elif effect == "chase_slow":
        return _fx_chase(hue, bri, bpm * 0.5, now)
    elif effect == "breathe":
        return _fx_breathe(hue, bri, now)
    elif effect == "ripple":
        return _fx_ripple(hue, bri, bpm, now)
    elif effect == "sparkle":
        return _fx_sparkle(hue, bri, bpm, now)
    else:  # gradient, default
        return _fx_gradient(hue, bri, now)


def _fx_breathe(hue: float, bri: float, now: float) -> np.ndarray:
    wave = 0.5 + 0.5 * math.sin(now * math.pi * 0.8)
    b    = bri * (0.3 + 0.7 * wave)
    px   = np.zeros((LED_COUNT, 3), dtype=np.uint8)
    px[:] = _hsv(hue, 0.9, b)
    return px


def _fx_gradient(hue: float, bri: float, now: float) -> np.ndarray:
    hue2 = (hue + 30) % 360
    c1   = np.array(_hsv(hue,  0.9, bri))
    c2   = np.array(_hsv(hue2, 0.7, bri * 0.6))
    t    = np.linspace(0, 1, LED_COUNT)[:, None]
    return (c1 * (1 - t) + c2 * t).astype(np.uint8)


def _fx_chase(hue: float, bri: float, bpm: float, now: float) -> np.ndarray:
    n     = LED_COUNT
    speed = 60.0   # LEDs per beat
    pos   = (now * bpm / 60.0 * speed) % n
    width = 80

    idx   = np.arange(n, dtype=np.float32)
    dist  = np.minimum((idx - pos) % n, (pos - idx) % n)
    gauss = np.exp(-dist**2 / (2 * (width * 0.5)**2))

    r, g, b = _hsv(hue, 0.95, bri)
    px = np.zeros((n, 3), dtype=np.uint8)
    px[:, 0] = (r * gauss).astype(np.uint8)
    px[:, 1] = (g * gauss).astype(np.uint8)
    px[:, 2] = (b * gauss).astype(np.uint8)
    return px


def _fx_pulse_chase(hue: float, bri: float, bpm: float, now: float) -> np.ndarray:
    n     = LED_COUNT
    speed = 90.0
    pos1  = (now * bpm / 60.0 * speed) % n
    pos2  = n - pos1
    hue2  = (hue + 180) % 360
    width = 60

    idx  = np.arange(n, dtype=np.float32)
    px   = np.zeros((n, 3), dtype=np.float32)
    for pos, h in [(pos1, hue), (pos2, hue2)]:
        dist  = np.minimum((idx - pos) % n, (pos - idx) % n)
        gauss = np.exp(-dist**2 / (2 * (width * 0.5)**2))
        r, g, b = _hsv(h, 1.0, bri)
        px[:, 0] += r * gauss
        px[:, 1] += g * gauss
        px[:, 2] += b * gauss

    return np.clip(px, 0, 255).astype(np.uint8)


def _fx_sparkle(hue: float, bri: float, bpm: float, now: float) -> np.ndarray:
    n  = LED_COUNT
    px = np.zeros((n, 3), dtype=np.uint8)
    # Dim background gradient
    hue2 = (hue + 40) % 360
    c1 = np.array(_hsv(hue,  0.9, bri * 0.3))
    c2 = np.array(_hsv(hue2, 0.7, bri * 0.15))
    t  = np.linspace(0, 1, n)[:, None]
    px[:] = (c1 * (1 - t) + c2 * t).astype(np.uint8)
    # Bright sparkle dots — seed changes 4× per beat
    rng = random.Random(int(now * bpm / 60.0 * 4))
    r, g, b = _hsv(hue, 0.4, min(1.0, bri * 1.5))
    for _ in range(n // 12):
        i = rng.randint(0, n - 1)
        px[i] = [r, g, b]
    return px


def _fx_ripple(hue: float, bri: float, bpm: float, now: float) -> np.ndarray:
    n      = LED_COUNT
    center = n // 2
    cycle  = 60.0 / bpm * 4
    phase  = (now % cycle) / cycle
    radius = phase * (n / 2)
    width  = 40

    idx   = np.arange(n, dtype=np.float32)
    dist  = np.abs(idx - center) - radius
    gauss = np.exp(-(dist**2) / (width**2))

    r, g, b = _hsv(hue, 0.85, bri)
    px = np.zeros((n, 3), dtype=np.uint8)
    px[:, 0] = (r * gauss).astype(np.uint8)
    px[:, 1] = (g * gauss).astype(np.uint8)
    px[:, 2] = (b * gauss).astype(np.uint8)
    return px
