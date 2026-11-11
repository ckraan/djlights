"""choreography_manager.py — Dirigent tussen track-plan en AnimationEngine.

Architectuur: drie lagen van audio-reactiviteit
────────────────────────────────────────────────
Base  Layer (The Mood)
    Phrase (intro/verse/up/chorus/down/outro) bepaalt:
    • palette-keuze en noise-snelheid (noise_rate)
    • density (negatieve ruimte: hoeveel LEDs actief)

Mid   Layer (The Energy)
    Waveform-envelope (bass/mid/treble uit PlayerSnapshot) moduleert de
    globale helderheid via brightness_mul in real-time.

Top   Layer (The Accents)
    Op elke beat (en bij keypoints/drops) worden kortstondige subpixel_dot
    accenten gespawnd boven op de noise-basis.

Musical Physics (Inertia)
    Alle doelwaarden (brightness, speed, density, noise_rate) worden via
    exponential decay vloeiend benaderd — geen harde parameter-sprongen.

Negative Space
    density < 1.0 laat ChoreographyManager het coherente noise-masker in
    AnimationEngine activeren, zodat rustige segmenten grote donkere zones
    bevatten.

Hard Cut
    Bij een drop-overgang (phrase → chorus of up) volgt een ~100ms blackout
    (visuele reset), daarna lopen de parameters via inertia in.

Beat-Synced Smoothing
    AnimationEngine.render() ontvangt het huidige BPM zodat de Simplex noise
    één cyclus per maat aflegt — de lichtshow 'ademt' op de maat.

Gebruik
───────
    from choreography_manager import ChoreographyManager

    mgr = ChoreographyManager(n_leds=1500)

    # In de render-loop (44 fps):
    frame = mgr.render(snap, t, palette_fn)
    ddp_sender.send(frame, ip)

    # Optioneel: extern plan doorgeven (AI-track-plan dict → {phrase_idx: {...}})
    mgr.apply_plan(track_id, plan_dict)

    # Extern beat-event (BLT OSC):
    mgr.on_beat(beat_number)
"""
from __future__ import annotations

import math
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from animation_engine import AnimationEngine, apply_palette

_log = logging.getLogger("choreography")

# ─────────────────────────────────────────────────────────────────────────────
# Phrase → parameter-doelen
# ─────────────────────────────────────────────────────────────────────────────

# noise_rate: lagere waarde = trage, diepe golven; hogere = snelle energie.
# density:    fractie van LEDs die oplicht.  1.0 = volledig strip.
# brightness: basishelderheid (voor waveform-modulatie er bovenop).
# speed:      AnimationEngine.speed (schaal op noise en accent beweging).
# hard_cut:   True → ~100ms blackout bij ingang van deze phrase.

_ALL_STRIPS = [0, 1, 2, 3, 4]

_PHRASE_PARAMS: dict[str, dict] = {
    #          brightness  speed  density  noise_rate  hard_cut  active_strips          mode
    "intro":  {"brightness": 0.12, "speed": 0.45, "density": 0.28, "noise_rate": 0.30, "hard_cut": False,
               "active_strips": [0, 2, 4],    "mode": "digital_rain"},
    "verse":  {"brightness": 0.20, "speed": 0.75, "density": 0.50, "noise_rate": 0.55, "hard_cut": False,
               "active_strips": [0, 2, 4],    "mode": "mirror_symmetry"},
    "up":     {"brightness": 0.22, "speed": 1.20, "density": 0.90, "noise_rate": 0.90, "hard_cut": True,
               "active_strips": _ALL_STRIPS,  "mode": "diamond_chase"},
    "chorus": {"brightness": 0.25, "speed": 1.80, "density": 1.00, "noise_rate": 1.40, "hard_cut": True,
               "active_strips": _ALL_STRIPS,  "mode": "diamond_bloom"},
    "down":   {"brightness": 0.14, "speed": 0.50, "density": 0.38, "noise_rate": 0.35, "hard_cut": False,
               "active_strips": [0, 2, 4],     "mode": "minimal_ambient"},
    "bridge": {"brightness": 0.18, "speed": 0.85, "density": 0.45, "noise_rate": 0.60, "hard_cut": False,
               "active_strips": [1, 3],        "mode": "ember_drift"},
    "outro":  {"brightness": 0.10, "speed": 0.30, "density": 0.18, "noise_rate": 0.20, "hard_cut": False,
               "active_strips": [0, 2, 4],     "mode": "deep_void"},
}

# Decays (τ = 1/decay seconden).  Snellere decay = harder/responsiever.
_DECAY_BRIGHTNESS = 5.0   # τ ≈ 200ms
_DECAY_SPEED      = 3.0   # τ ≈ 333ms  (snelheidsverandering voelt muzikaler trager aan)
_DECAY_DENSITY    = 2.0   # τ ≈ 500ms  (negatieve ruimte opent/sluit langzaam)
_DECAY_NOISE_RATE = 4.0   # τ ≈ 250ms

# Hard-cut duur in seconden
_HARD_CUT_DURATION = 0.100   # 100ms blackout

# ─────────────────────────────────────────────────────────────────────────────
# Per-strip spatial diversity targets
# ─────────────────────────────────────────────────────────────────────────────
# spread     : max LED offset between outermost and innermost strips
# spd_lo/hi  : speed_mul range — outer-left strip gets spd_lo, outer-right spd_hi
# reverse    : which strip indices run backwards through their segment
# mirror     : {dst: src} — dst strip mirrors (copies + flips) src strip
#
# Mid-layer waveform energy widens the spread further (up to +50%).
# Bass energy pumps the outer strip speeds.
# These are targets; actual values drift via _Smooth inertia.

_STRIP_DIVERSITY: dict[str, dict] = {
    "intro":  {"spread": 15,  "spd_lo": 0.90, "spd_hi": 1.10,
               "reverse": [],        "mirror": {0: 4, 1: 3}},
    "verse":  {"spread": 60,  "spd_lo": 0.72, "spd_hi": 1.32,
               "reverse": [1, 3],    "mirror": {}},
    "up":     {"spread": 100, "spd_lo": 0.58, "spd_hi": 1.65,
               "reverse": [0, 2, 4], "mirror": {}},
    "chorus": {"spread": 145, "spd_lo": 0.48, "spd_hi": 2.05,
               "reverse": [1, 3],    "mirror": {0: 4, 1: 3}},
    "down":   {"spread": 22,  "spd_lo": 0.88, "spd_hi": 1.14,
               "reverse": [],        "mirror": {}},
    "bridge": {"spread": 70,  "spd_lo": 0.68, "spd_hi": 1.42,
               "reverse": [0, 4],    "mirror": {1: 3}},
    "outro":  {"spread": 10,  "spd_lo": 0.95, "spd_hi": 1.05,
               "reverse": [],        "mirror": {0: 4}},
}

# Normalized position of each strip in the speed range:
# 0.0 = gets spd_lo, 0.5 = neutral (1.0), 1.0 = gets spd_hi.
# Outer-left is slow, center is 1.0, outer-right is fast.
_STRIP_SPD_POS  = [0.00, 0.25, 0.50, 0.75, 1.00]
_STRIP_OFF_FRAC = [-0.50, -0.25, 0.00, 0.25, 0.50]  # fraction of spread per strip


# ─────────────────────────────────────────────────────────────────────────────
# Exponential-decay parameter smoother
# ─────────────────────────────────────────────────────────────────────────────

class _Smooth:
    """Vloeiende parameter via exponential decay.

    step(dt) bewegt .cur naar .target met tijdconstante τ = 1/decay.
    """

    __slots__ = ("cur", "target", "decay")

    def __init__(self, value: float, decay: float) -> None:
        self.cur    = float(value)
        self.target = float(value)
        self.decay  = float(decay)

    def set_target(self, value: float) -> None:
        self.target = float(value)

    def step(self, dt: float) -> float:
        alpha    = 1.0 - math.exp(-self.decay * max(dt, 0.0))
        self.cur += (self.target - self.cur) * alpha
        return self.cur

    def snap(self, value: float) -> None:
        """Direct zetten zonder inertia (bijv. na hard cut)."""
        self.cur = self.target = float(value)


# ─────────────────────────────────────────────────────────────────────────────
# Accent-dot (subpixel tactiele puls)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _AccentDot:
    pos     : float       # LED-positie (float, 0..n-1)
    color   : tuple       # (r, g, b) in [0, 1]
    sigma   : float       # Gaussische breedte in LEDs
    born    : float       # time.perf_counter() bij spawnen
    lifetime: float       # seconden dat de dot zichtbaar is


# ─────────────────────────────────────────────────────────────────────────────
# ChoreographyManager
# ─────────────────────────────────────────────────────────────────────────────

class ChoreographyManager:
    """Dirigent: vertaalt PlayerSnapshot + track-plan → AnimationEngine frame.

    Parameters
    ----------
    n_leds        : aantal LEDs van de strip.
    fps           : verwachte frame-rate (gebruikt voor dt-berekening).
    palette_name  : standaard-palette als geen externe palette_fn wordt gegeven.
    """

    # Accent dot eigenschappen per phrase
    _ACCENT_SIGMA: dict[str, float] = {
        "intro":  2.5,
        "verse":  2.0,
        "up":     1.5,
        "chorus": 1.2,   # hard, tactiel
        "down":   3.0,
        "bridge": 2.2,
        "outro":  3.5,
    }
    # Aantal accenten per beat (chorus = meer chaos)
    _ACCENT_COUNT: dict[str, int] = {
        "intro": 1, "verse": 1, "up": 2, "chorus": 3,
        "down": 1, "bridge": 1, "outro": 1,
    }
    # Levensduur van accent-dots in seconden
    _ACCENT_LIFETIME: dict[str, float] = {
        "intro": 0.25, "verse": 0.20, "up": 0.18, "chorus": 0.14,
        "down": 0.30, "bridge": 0.22, "outro": 0.35,
    }

    def __init__(
        self,
        n_leds       : int   = 1500,
        fps          : int   = 44,
        palette_name : str   = "aurora",
    ) -> None:
        self.n   = n_leds
        self.fps = fps

        # AnimationEngine (float32 pipeline + gamma LUT)
        self._engine = AnimationEngine(n_leds=n_leds, fps=fps,
                                       palette_name=palette_name)

        # ── Inertia-parameters ─────────────────────────────────────────────
        # Initieel op "intro"-waarden
        _p0 = _PHRASE_PARAMS["intro"]
        self._brightness  = _Smooth(_p0["brightness"],  _DECAY_BRIGHTNESS)
        self._speed       = _Smooth(_p0["speed"],       _DECAY_SPEED)
        self._density     = _Smooth(_p0["density"],     _DECAY_DENSITY)
        self._noise_rate  = _Smooth(_p0["noise_rate"],  _DECAY_NOISE_RATE)

        # ── State ──────────────────────────────────────────────────────────
        self._current_phrase : str        = ""
        self._current_beat   : int        = -1
        self._last_render_t  : float      = time.perf_counter()

        # Active strips: welke van de 5 strips branden.  Wisselt op phrase-boundary
        # (na hard cut) — zodat de strip-switch altijd op een muzikaal moment valt.
        self._active_strips: list[int] = list(_PHRASE_PARAMS["intro"]["active_strips"])

        # Club-mode: schakelt discreet op phrase-boundary
        self._mode: str = _PHRASE_PARAMS["intro"].get("mode", "noise")

        # Hard-cut window: wanneer time.perf_counter() < _hard_cut_until,
        # geeft render() een zwart frame terug.
        self._hard_cut_until : float = 0.0

        # Actieve accent-dots
        self._accent_dots: list[_AccentDot] = []

        # AI track-plan:  { track_id → { phrase_idx → {...} } }
        self._plans: dict[int, dict] = {}

        # UI-readable state from last plan entry (shown in status bar)
        self._current_palette      : str = "aurora"
        self._current_direction    : str = "forward"
        self._current_grouping     : int = 1
        self._current_spacing      : int = 0
        self._current_effect_name  : str = ""   # plan "effect" field for UI highlight
        self._current_back_accent  : str = "off" # plan back_accent driving CaveStripRenderer

        # ── Per-strip spatial diversity ────────────────────────────────────
        # Each of the 5 strips gets its own smoothed offset and speed_mul.
        # Discrete params (reverse, mirror_of) switch on phrase boundary.
        self._strip_offsets  = [_Smooth(0.0, decay=0.8) for _ in range(5)]
        self._strip_speeds   = [_Smooth(1.0, decay=1.5) for _ in range(5)]
        self._strip_reverse  = [False] * 5
        self._strip_mirror   : list = [None] * 5   # list[int | None]
        # Initialise targets from intro diversity
        self._apply_diversity_targets(
            _STRIP_DIVERSITY["intro"], bass=0.0, mid=0.0)

    # ─────────────────────────────────────────────────────────────────────
    # Publieke API
    # ─────────────────────────────────────────────────────────────────────

    def apply_plan(self, track_id: int, plan: dict) -> None:
        """Sla een AI-gegenereerd track-plan op (optioneel).

        plan-formaat: { phrase_idx: {"brightness": 0.22, "speed": 1.1, ...} }
        Wanneer een phrase opgezocht wordt, worden plan-overrides op de
        _PHRASE_PARAMS basis-waarden gelegd.
        """
        self._plans[track_id] = plan
        _log.debug("ChoreographyManager: plan opgeslagen voor track %d (%d phrases)",
                   track_id, len(plan))

    def on_beat(self, beat_number: int, bpm: float = 120.0,
                palette_fn=None) -> None:
        """Roep aan bij elk nieuw OSC beat-event.

        Spawnt accent-dots voor de actieve phrase.  palette_fn wordt gebruikt
        om een kleur te kiezen voor de dot (anders: wit).
        """
        if beat_number == self._current_beat:
            return
        self._current_beat = beat_number

        phrase = self._current_phrase or "verse"
        n_dots = self._ACCENT_COUNT.get(phrase, 1)
        sigma  = self._ACCENT_SIGMA.get(phrase, 2.0)
        life   = self._ACCENT_LIFETIME.get(phrase, 0.20)
        now    = time.perf_counter()

        for _ in range(n_dots):
            pos = random.uniform(0.05, 0.95) * (self.n - 1)
            if palette_fn is not None:
                hue = random.random()
                rgb = palette_fn(hue)
                # Accent iets feller dan base (1.2× boost, clip via render)
                color = (min(1.0, rgb[0] * 1.2),
                         min(1.0, rgb[1] * 1.2),
                         min(1.0, rgb[2] * 1.2))
            else:
                color = (1.0, 1.0, 1.0)
            self._accent_dots.append(
                _AccentDot(pos=pos, color=color, sigma=sigma,
                           born=now, lifetime=life)
            )

    def render(
        self,
        snap       : "Optional[PlayerSnapshot]",
        t          : float,
        palette_fn = None,
    ) -> np.ndarray:
        """Produceer een (n, 3) uint8 frame.

        Parameters
        ----------
        snap       : PlayerSnapshot of None.  Wanneer None wordt het laatste
                     bekende state voortgezet.
        t          : huidige track-positie in seconden (dead-reckoned van BLT).
        palette_fn : callable(hue:float) → (r,g,b) floats in [0,255].
                     Wanneer None: engine gebruikt intern palette.
        """
        now = time.perf_counter()
        dt  = max(0.001, now - self._last_render_t)
        self._last_render_t = now

        bpm = 120.0
        bass = mid_e = 0.0
        if snap is not None:
            bpm   = float(snap.bpm) if snap.bpm > 0 else 120.0
            bass  = float(snap.bass_energy)
            mid_e = float(snap.mid_energy)
            self._update_from_snap(snap, now, bpm, palette_fn)

        # Per-strip diversity targets: update elke frame VOOR de hard-cut check
        # zodat ook drop-phrases (chorus/up) hun targets direct instellen.
        div = _STRIP_DIVERSITY.get(self._current_phrase or "intro",
                                   _STRIP_DIVERSITY["intro"])
        self._apply_diversity_targets(div, bass, mid_e)

        # ── Hard Cut: blackout ~100ms ──────────────────────────────────────
        if now < self._hard_cut_until:
            return np.zeros((self.n, 3), dtype=np.uint8)

        # ── Stap inertia-parameters ────────────────────────────────────────
        brightness = self._brightness.step(dt)
        speed      = self._speed.step(dt)
        density    = self._density.step(dt)
        noise_rate = self._noise_rate.step(dt)

        # Synchroniseer AnimationEngine.speed met de gesmoothe waarde
        self._engine.speed = np.float32(speed)
        self._engine.brightness = np.float32(brightness)

        # ── Mid Layer: waveform-energie → brightness_mul ─────────────────
        brightness_mul = 1.0
        treble = 0.0
        if snap is not None:
            treble  = float(snap.treble_energy)
            energy  = bass * 0.55 + mid_e * 0.30 + treble * 0.15
            brightness_mul = max(0.70, min(1.30, 0.70 + energy * 0.60))

        # ── Top Layer: accent dots — verwijder verlopen ────────────────────
        live_dots: list[tuple] = []
        for dot in self._accent_dots:
            age  = now - dot.born
            frac = age / dot.lifetime   # 0.0 = vers, 1.0 = verlopen
            if frac >= 1.0:
                continue
            # Fade-out: amplitude daalt van 1.0 → 0.0 (kwadratisch)
            amp = (1.0 - frac) ** 2
            r, g, b = dot.color
            live_dots.append((dot.pos, (r * amp, g * amp, b * amp), dot.sigma))
        self._accent_dots = [d for d in self._accent_dots
                             if (now - d.born) < d.lifetime]

        # ── Per-strip params: stap smoothers en bouw dict-lijst ───────────
        strip_params = []
        for s in range(5):
            off = self._strip_offsets[s].step(dt)
            spd = self._strip_speeds[s].step(dt)
            strip_params.append({
                "offset":    off,
                "speed_mul": spd,
                "reverse":   self._strip_reverse[s],
                "mirror_of": self._strip_mirror[s],
                "grouping":  self._current_grouping,
                "spacing":   self._current_spacing,
            })

        # ── Render via AnimationEngine ─────────────────────────────────────
        return self._engine.render(
            t             = t,
            density       = float(density),
            brightness_mul= float(brightness_mul),
            noise_rate    = float(noise_rate),
            bpm           = bpm,
            active_strips = self._active_strips,
            accent_dots   = live_dots if live_dots else None,
            mode          = self._mode,
            strip_params  = strip_params,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Intern: snapshot verwerken
    # ─────────────────────────────────────────────────────────────────────

    def _phrase_base(self, kind: str) -> str:
        """Strip nummeriek achtervoegsel van BLT phrase-kind (bijv. 'up_1' → 'up')."""
        if not kind:
            return ""
        base = kind.rsplit("_", 1)[0]
        return base if base in _PHRASE_PARAMS else kind

    def _update_from_snap(
        self,
        snap,
        now        : float,
        bpm        : float,
        palette_fn,
    ) -> None:
        """Detecteer phrase-wijzigingen, zet doelparameters, trigger hard cut."""

        # ── Bepaal actieve phrase ──────────────────────────────────────────
        cp = getattr(snap, "current_phrase", None)
        raw_kind = cp.kind if cp else ""
        phrase   = self._phrase_base(raw_kind)
        # Alias: bridge → down (als in wled_ctrl.py)
        if phrase == "bridge":
            phrase = "down"

        phrase_changed = (phrase != self._current_phrase) and (phrase != "")

        if phrase_changed:
            self._on_phrase_change(phrase, snap, now, bpm, palette_fn)
            self._current_phrase = phrase

        # ── Beat-change detectie → accent dot spawnen ─────────────────────
        beat_n = getattr(snap, "beat_number", 0) or 0
        if beat_n != self._current_beat and beat_n > 0:
            self.on_beat(beat_n, bpm=bpm, palette_fn=palette_fn)
            self._check_mid_phrase_changes(beat_n)

        # ── AI plan-override (optioneel) ───────────────────────────────────
        if phrase_changed and phrase:
            track_id = getattr(snap, "track_id", 0) or 0
            plan = self._plans.get(track_id)
            if plan:
                self._apply_plan_overrides(plan, snap, phrase)

    def _check_mid_phrase_changes(self, beat_n: int) -> None:
        """Trigger mid_phrase_changes die op of voor beat_n vallen."""
        mid_changes = getattr(self, "_mid_changes", {})
        last_applied = getattr(self, "_last_mid_applied", -1)
        for trigger_beat in sorted(mid_changes):
            if trigger_beat > last_applied and beat_n >= trigger_beat:
                self._apply_plan_entry(mid_changes[trigger_beat])
                self._last_mid_applied = trigger_beat
                _log.debug("CM mid_phrase_change fired at beat %d", trigger_beat)

    def _apply_diversity_targets(
        self,
        div  : dict,
        bass : float,
        mid  : float,
    ) -> None:
        """Bereken en stel nieuwe doelwaarden in voor per-strip offset en speed.

        mid-energie verbreedt de ruimtelijke spreiding.
        bass-energie pompt de snelheid van de buitenste strips op.
        """
        spread = div["spread"] * (1.0 + mid * 0.50)   # mid widens spread
        spd_lo = div["spd_lo"]
        spd_hi = div["spd_hi"]
        bass_boost = 1.0 + bass * 0.28                 # bass pumps outer strips

        for s in range(5):
            pos = _STRIP_SPD_POS[s]
            # Interpoleer speed_mul: 0→spd_lo, 0.5→1.0, 1→spd_hi
            if pos <= 0.5:
                spd = spd_lo + (1.0 - spd_lo) * (pos * 2.0)
            else:
                spd = 1.0 + (spd_hi - 1.0) * ((pos - 0.5) * 2.0)
            # Outer strips (0 and 4) get a bass-energy speed kick
            if s in (0, 4):
                spd *= bass_boost

            self._strip_speeds[s].set_target(spd)
            self._strip_offsets[s].set_target(_STRIP_OFF_FRAC[s] * spread)

    def _on_phrase_change(
        self,
        new_phrase : str,
        snap,
        now        : float,
        bpm        : float,
        palette_fn,
    ) -> None:
        """Stel nieuwe parameter-doelen in en activeer hard cut indien nodig."""
        params = _PHRASE_PARAMS.get(new_phrase, _PHRASE_PARAMS["verse"])

        _log.info("ChoreographyManager: phrase %s→%s  brightness=%.2f  "
                  "speed=%.2f  density=%.2f  noise_rate=%.2f",
                  self._current_phrase, new_phrase,
                  params["brightness"], params["speed"],
                  params["density"], params["noise_rate"])

        self._brightness.set_target(params["brightness"])
        self._speed.set_target(params["speed"])
        self._density.set_target(params["density"])
        self._noise_rate.set_target(params["noise_rate"])

        # Hard cut op drops (chorus/up) — visuele reset
        if params.get("hard_cut"):
            self._hard_cut_until = now + _HARD_CUT_DURATION
            _log.debug("ChoreographyManager: hard cut %.0fms voor phrase '%s'",
                       _HARD_CUT_DURATION * 1000, new_phrase)
            # Na de hard cut: snap parameters direct naar doel zodat de
            # inertia niet vanuit de vorige phrase-waarden begint.
            # (Geeft een krachtige binnenkomst in plaats van fade-in.)
            self._brightness.snap(params["brightness"])
            self._speed.snap(params["speed"])
            self._density.snap(params["density"])
            self._noise_rate.snap(params["noise_rate"])

        # Wissel actieve strips — snap direct (na hard cut is dit de gewenste
        # krachtige wisseling; zonder hard cut gaat het geleidelijk via density).
        new_strips = params.get("active_strips", _ALL_STRIPS)
        self._active_strips = list(new_strips)
        _log.debug("ChoreographyManager: active_strips → %s", self._active_strips)

        # Club-mode: schakel discreet op phrase-boundary
        new_mode = params.get("mode", "noise")
        if new_mode != self._mode:
            self._mode = new_mode
            _log.debug("ChoreographyManager: mode → %s", self._mode)

        # Per-strip discrete params: reverse en mirror wisselen op phrase-boundary
        div = _STRIP_DIVERSITY.get(new_phrase, _STRIP_DIVERSITY["verse"])
        rev_set = set(div.get("reverse", []))
        mir_map = div.get("mirror", {})
        for s in range(5):
            self._strip_reverse[s] = s in rev_set
            self._strip_mirror[s]  = mir_map.get(s)
        _log.debug("ChoreographyManager: strip_reverse=%s mirror=%s",
                   self._strip_reverse, self._strip_mirror)

        # Wissel intern palette
        _PHRASE_PALETTES = {
            "intro":  "aurora",
            "verse":  "ocean",
            "up":     "fire",
            "chorus": "fire",
            "down":   "aurora",
            "outro":  "aurora",
        }
        if new_phrase in _PHRASE_PALETTES:
            eng_pal = _PHRASE_PALETTES[new_phrase]
            self._engine.set_palette(eng_pal)
            self._current_palette = eng_pal
        # Reset to defaults on phrase start (plan entry applied immediately after)
        self._current_direction = "forward"
        self._current_back_accent = "off"

    # Map plan effect names → AnimationEngine modes
    _EFFECT_MODE_MAP: dict = {
        # intro / atmospheric
        "nightsky":        "digital_rain",
        "trailingoff":     "deep_void",
        "aurorawave":      "minimal_ambient",
        "emberdrift":      "ember_drift",
        # verse
        "rhythmblocks":    "mirror_symmetry",
        "meteorshower":    "noise",
        # up / buildup — three distinct modes
        "pressurebeams":   "diamond_wave",     # up1: growing wave
        "escalatorsteps":  "diamond_chase",    # up2: traveling chase
        "turbinebeam":     "diamond_bloom",    # up3: pre-finale bloom
        # chorus / drop — three distinct modes
        "impactwaves":     "diamond_bloom",    # chorus1: expanding bloom
        "chromarush":      "diamond_chase",    # chorus2: traveling chase
        "beatstrobe":      "vertical_pulse",   # chorus3 finale: stroby pulse
        # generic fallbacks
        "interstitialchase": "interstitial_chase",
        "verticalpulse":     "vertical_pulse",
        "digitalrain":       "digital_rain",
        "mirrorsymmetry":    "mirror_symmetry",
    }

    # Map plan palette names → AnimationEngine palette names
    _PALETTE_MAP: dict = {
        "fire": "fire", "crimson": "fire", "darkred": "fire",
        "sunset": "fire", "orange": "fire", "amber": "fire",
        "ocean": "ocean", "deepblue": "ocean", "deephouse": "ocean",
        "blueonly": "ocean", "purple": "ocean",
        "neon": "neon", "cyberpunk": "neon", "pink": "neon",
        "contrast": "neon",
        "aurora": "aurora", "violet": "aurora", "monochrome": "aurora",
    }

    def _apply_plan_entry(self, entry: dict) -> None:
        """Pas één plan-entry of mid_phrase_change toe op de huidige staat.

        Ondersteunde keys:
          brightness, speed, density, noise_rate, palette
          active_strips (1-indexed → 0-indexed)
          strip_offset, strip_speed_mul, strip_reverse, strip_mirror
        """
        if "mode" in entry:
            new_mode = str(entry["mode"])
            if new_mode != self._mode:
                self._mode = new_mode
                _log.debug("CM plan mode → %s", new_mode)
        elif "effect" in entry:
            # Map plan effect name to engine mode (mode key takes priority if both present)
            eff_key = str(entry["effect"]).lower().replace(" ", "").replace("_", "")
            mapped  = self._EFFECT_MODE_MAP.get(eff_key)
            if mapped and mapped != self._mode:
                self._mode = mapped
                _log.debug("CM plan effect '%s' → mode %s", entry["effect"], mapped)

        if "effect" in entry:
            self._current_effect_name = str(entry["effect"])

        # back_accent: persistent mode for CaveStripRenderer
        if "back_accent" in entry:
            self._current_back_accent = str(entry["back_accent"])
            _log.debug("CM plan back_accent → %s", self._current_back_accent)
        # back_trigger: one-shot escalation (overrides back_accent for remainder of phrase)
        if "back_trigger" in entry:
            self._current_back_accent = str(entry["back_trigger"])
            _log.debug("CM plan back_trigger → %s (escalation)", self._current_back_accent)

        if "palette" in entry:
            pal_key = str(entry["palette"]).lower().replace(" ", "").replace("_", "")
            eng_pal = self._PALETTE_MAP.get(pal_key, "aurora")
            self._engine.set_palette(eng_pal)
            self._current_palette = str(entry["palette"])   # human-readable for UI
            _log.debug("CM plan palette '%s' → engine '%s'", entry["palette"], eng_pal)

        if "direction" in entry:
            d = str(entry["direction"]).lower()
            self._current_direction = d
            if d == "forward":
                for s in range(5):
                    self._strip_reverse[s] = False
                for s in range(5):
                    self._strip_mirror[s] = None
            elif d == "reverse":
                for s in self._active_strips:
                    self._strip_reverse[s] = True
            elif d == "mirror":
                # Strips 0,1 mirror strips 4,3; strip 2 is the source
                for s in range(5):
                    self._strip_reverse[s] = False
                self._strip_mirror = [4, 3, None, None, None]

        if "grouping" in entry:
            if not getattr(self, "_manual_grouping", False):
                self._current_grouping = int(entry["grouping"])

        if "spacing" in entry:
            if not getattr(self, "_manual_spacing", False):
                self._current_spacing = int(entry["spacing"])

        if "brightness" in entry:
            self._brightness.set_target(float(entry["brightness"]))
        if "speed" in entry:
            self._speed.set_target(float(entry["speed"]))
        if "density" in entry:
            self._density.set_target(float(entry["density"]))
        if "noise_rate" in entry:
            self._noise_rate.set_target(float(entry["noise_rate"]))

        # active_strips: plan uses 1-indexed, engine uses 0-indexed
        if "active_strips" in entry:
            raw = entry["active_strips"]
            if isinstance(raw, list):
                self._active_strips = [int(s) - 1 for s in raw
                                       if isinstance(s, (int, float)) and 1 <= s <= 5]
            _log.debug("CM plan active_strips → %s", self._active_strips)

        # Per-strip spatial diversity from plan (override phrase-default targets)
        # snap=True on phrase boundaries so offsets/speeds take effect immediately
        # instead of drifting via inertia over the full phrase duration.
        _snap_divs = getattr(self, "_snap_diversity", False)
        if "strip_offset" in entry:
            for s, val in enumerate(entry["strip_offset"][:5]):
                if val is not None:
                    if _snap_divs:
                        self._strip_offsets[s].snap(float(val))
                    else:
                        self._strip_offsets[s].set_target(float(val))
        if "strip_speed_mul" in entry:
            for s, val in enumerate(entry["strip_speed_mul"][:5]):
                if val is not None:
                    if _snap_divs:
                        self._strip_speeds[s].snap(float(val))
                    else:
                        self._strip_speeds[s].set_target(float(val))
        if "strip_reverse" in entry:
            for s, val in enumerate(entry["strip_reverse"][:5]):
                self._strip_reverse[s] = bool(val)
        if "strip_mirror" in entry:
            for s, val in enumerate(entry["strip_mirror"][:5]):
                # plan uses 1-indexed; convert to 0-indexed (None stays None)
                self._strip_mirror[s] = (int(val) - 1) if val is not None else None

    def _apply_plan_overrides(self, plan: dict, snap, phrase: str) -> None:
        """Zoek het meest passende plan-entry en pas het toe.  Laadt ook
        mid_phrase_changes zodat ze op beat-boundary kunnen worden getriggerd.
        """
        cp = getattr(snap, "current_phrase", None)
        if cp is None:
            return
        beat_start = cp.beat_start

        best_key, best_delta = None, float("inf")
        for k, entry in plan.items():
            delta = abs(entry.get("beat_start", 0) - beat_start)
            if delta < best_delta:
                best_delta, best_key = delta, k
        if best_key is None:
            return
        entry = plan[best_key]

        # Reset manual locks on phrase boundary so plan sets initial grouping/spacing
        self._manual_grouping = False
        self._manual_spacing  = False

        # Snap offsets/speeds on phrase boundary so they take effect immediately
        self._snap_diversity = True
        self._apply_plan_entry(entry)
        self._snap_diversity = False

        # Sla mid_phrase_changes op voor beat-triggered toepassing
        self._mid_changes: dict[int, dict] = {}
        for mc in entry.get("mid_phrase_changes", []):
            at = mc.get("at_beat")
            if at is not None:
                self._mid_changes[beat_start + int(at)] = mc
        self._last_mid_applied: int = -1

        _log.debug("CM plan '%s' geladen: %d mid_changes", phrase,
                   len(self._mid_changes))

    # ─────────────────────────────────────────────────────────────────────
    # Debug / diagnostics
    # ─────────────────────────────────────────────────────────────────────

    def state_summary(self) -> str:
        """Eenregelige status voor diagnostische logging."""
        hc = max(0.0, self._hard_cut_until - time.perf_counter())
        act_s = ",".join(str(s + 1) for s in self._active_strips)
        dir_abbr = {"forward": "FWD", "reverse": "REV", "mirror": "MIR"}.get(
            self._current_direction, self._current_direction[:3].upper()
        )
        return (
            f"phrase={self._current_phrase or '—':<7}  mode={self._mode:<16} "
            f"pal={self._current_palette:<10}  dir={dir_abbr}  "
            f"G{self._current_grouping} S{self._current_spacing}  "
            f"strips=[{act_s}]\n"
            f"bri={self._brightness.cur:.3f}→{self._brightness.target:.3f}  "
            f"spd={self._speed.cur:.2f}→{self._speed.target:.2f}  "
            f"den={self._density.cur:.2f}→{self._density.target:.2f}  "
            f"nr={self._noise_rate.cur:.2f}→{self._noise_rate.target:.2f}  "
            f"dots={len(self._accent_dots)}  hc={hc*1000:.0f}ms"
        )
