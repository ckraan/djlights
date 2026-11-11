"""animation_engine.py — Float32 animatie-pipeline voor 1500 LEDs.

Pipeline:
  1. Simplex Noise  → float32 waarden in [0, 1] over de strip
  2. Kleur-mapping  → (n, 3) float32 RGB via palette interpolatie
  3. Subpixel       → float posities antialiased over buurpixels
  4. Gamma LUT      → perceptueel lineaire helderheid (γ = 2.2)
  5. uint8 cast     → klaar voor DDPSender.send()

Gebruik:
    from animation_engine import AnimationEngine
    engine = AnimationEngine(n_leds=1500, fps=44)
    frame  = engine.render(t)          # (1500, 3) uint8
    sender.send(frame, "192.168.1.x")
"""
import time
import numpy as np
import opensimplex

# ─────────────────────────────────────────────────────────────────────────────
# Constanten
# ─────────────────────────────────────────────────────────────────────────────
N_LEDS   = 1500
GAMMA    = 2.2
LUT_SIZE = 4096   # hogere LUT-resolutie → minder banding bij donkere kleuren


# ─────────────────────────────────────────────────────────────────────────────
# 1. Gamma Correction Look-up Table
# ─────────────────────────────────────────────────────────────────────────────

def build_gamma_lut(gamma: float = GAMMA, size: int = LUT_SIZE) -> np.ndarray:
    """Bouw een float32 LUT: lineaire lichtsterkte → gecorrigeerde stuurwaarde.

    Ingang  : float32 [0, 1]  (lineaire fysieke intensiteit)
    Uitgang : float32 [0, 1]  (gamma-gecorrigeerde PWM-waarde)

    sRGB-benadering: y = x^(1/γ)  voor γ = 2.2
    Vermenigvuldig met 255 en cast naar uint8 als laatste stap.
    """
    x   = np.linspace(0.0, 1.0, size, dtype=np.float32)
    lut = np.power(np.clip(x, 0.0, 1.0), 1.0 / gamma, dtype=np.float32)
    return lut


def apply_gamma_lut(pixels: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Pas de gamma-LUT toe op een (n, 3) float32 array.

    Gebruikt integer-indexering in de LUT — sneller dan np.interp op elke
    kleurkanaal omdat het één enkel array-lookup is zonder interpolatie-overhead.
    """
    size   = len(lut)
    idx    = np.clip((pixels * (size - 1)).astype(np.int32), 0, size - 1)
    return lut[idx]   # (n, 3) float32


# ─────────────────────────────────────────────────────────────────────────────
# 2. Subpixel Rendering
# ─────────────────────────────────────────────────────────────────────────────

def subpixel_dot(canvas: np.ndarray, pos: float,
                 color: np.ndarray, sigma: float = 1.0) -> None:
    """Schrijf één punt op een float-positie met Gaussische verspreiding.

    Parameters
    ----------
    canvas : (n, 3) float32  — pixel-buffer, wordt in-place aangepast
    pos    : float           — LED-positie (bijv. 120.4)
    color  : (3,) float32    — RGB waarden in [0, 1]
    sigma  : float           — breedte van de Gaussische envelop in LEDs
                               sigma=1.0 ≈ harde dot, sigma=3.0 ≈ zachte glow
    """
    n       = canvas.shape[0]
    # Bepaal het relevante venster: ±4σ, afgerond naar integers
    radius  = int(np.ceil(sigma * 4))
    i_ctr   = int(pos)
    lo      = max(0, i_ctr - radius)
    hi      = min(n, i_ctr + radius + 1)
    if lo >= hi:
        return

    indices = np.arange(lo, hi, dtype=np.float32)
    gauss   = np.exp(-0.5 * ((indices - pos) / sigma) ** 2, dtype=np.float32)

    canvas[lo:hi, 0] += color[0] * gauss
    canvas[lo:hi, 1] += color[1] * gauss
    canvas[lo:hi, 2] += color[2] * gauss


def subpixel_line(canvas: np.ndarray, positions: np.ndarray,
                  colors: np.ndarray, sigma: float = 1.0) -> None:
    """Vectorized versie voor meerdere punten tegelijk.

    Parameters
    ----------
    positions : (m,) float32  — m float-posities
    colors    : (m, 3) float32
    """
    n = canvas.shape[0]
    arr = np.arange(n, dtype=np.float32)   # (n,)

    for i, (pos, col) in enumerate(zip(positions, colors)):
        dist  = arr - float(pos)           # (n,)
        gauss = np.exp(-0.5 * (dist / sigma) ** 2, dtype=np.float32)
        canvas[:, 0] += col[0] * gauss
        canvas[:, 1] += col[1] * gauss
        canvas[:, 2] += col[2] * gauss


# ─────────────────────────────────────────────────────────────────────────────
# 3. Simplex Noise helpers
# ─────────────────────────────────────────────────────────────────────────────

def noise1d(n: int, t: float, spatial_scale: float = 0.008,
            time_scale: float = 0.4, seed: int = 0) -> np.ndarray:
    """Genereer een (n,) float32 Simplex-ruis array in [0, 1].

    Elke LED krijgt een 2D Simplex-sample (led_index × spatial_scale,
    t × time_scale), waardoor de ruis vloeiend door de ruimte én de tijd
    beweegt.

    Parameters
    ----------
    spatial_scale : lagere waarde → grotere 'golven' over de strip
    time_scale    : lagere waarde → langzamere tijdbeweging
    """
    opensimplex.seed(seed)
    positions = np.arange(n, dtype=np.float64) * spatial_scale
    t_val     = float(t) * time_scale

    # opensimplex werkt sample-per-sample; vectoriseer via list comprehension
    # (opensimplex heeft geen vectorized API, maar de lus is buiten NumPy
    # alleen n=1500 floats → <1 ms op moderne hardware)
    raw = np.array(
        [opensimplex.noise2(x, t_val) for x in positions],
        dtype=np.float32,
    )
    # Schaal van [-1, 1] naar [0, 1]
    return (raw + 1.0) * 0.5


def noise2d_rgb(n: int, t: float, spatial_scale: float = 0.006,
                time_scale: float = 0.3) -> np.ndarray:
    """Genereer een (n, 3) float32 kleur-array via drie onafhankelijke
    Simplex-lagen (R/G/B krijgen elk een andere seed en tijdfase)."""
    r = noise1d(n, t,           spatial_scale, time_scale, seed=1)
    g = noise1d(n, t + 100.0,   spatial_scale, time_scale, seed=2)
    b = noise1d(n, t + 200.0,   spatial_scale, time_scale, seed=3)
    return np.stack([r, g, b], axis=1)   # (n, 3) float32


# ─────────────────────────────────────────────────────────────────────────────
# 4. Palette-mapper
# ─────────────────────────────────────────────────────────────────────────────

def make_palette_lut(stops: list[tuple[float, tuple[int, int, int]]],
                     size: int = 256) -> np.ndarray:
    """Interpoleer kleurstops naar een (size, 3) float32 LUT.

    stops : [(pos, (r, g, b)), ...]  met pos in [0, 1], rgb in [0, 255]

    Voorbeeld:
        fire = make_palette_lut([
            (0.0, (0,   0,   0)),
            (0.3, (200, 0,   0)),
            (0.7, (255, 140, 0)),
            (1.0, (255, 255, 200)),
        ])
    """
    stops  = sorted(stops, key=lambda s: s[0])
    lut    = np.zeros((size, 3), dtype=np.float32)
    xs     = np.linspace(0.0, 1.0, size)

    for ch in range(3):
        stop_x = np.array([s[0]       for s in stops], dtype=np.float32)
        stop_v = np.array([s[1][ch] / 255.0 for s in stops], dtype=np.float32)
        lut[:, ch] = np.interp(xs, stop_x, stop_v).astype(np.float32)

    return lut


def apply_palette(values: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Map (n,) float32 waarden in [0,1] naar (n, 3) float32 RGB via LUT."""
    size = len(lut)
    idx  = np.clip((values * (size - 1)).astype(np.int32), 0, size - 1)
    return lut[idx]


# ─────────────────────────────────────────────────────────────────────────────
# 5. AnimationEngine — samengesteld
# ─────────────────────────────────────────────────────────────────────────────

class AnimationEngine:
    """Kant-en-klare float32 animatie-pipeline.

    Gebruik
    -------
        engine = AnimationEngine(n_leds=1500)
        # in je render-loop:
        frame = engine.render(t)   # (n, 3) uint8 → direct naar DDPSender
    """

    # Ingebouwde paletten
    PALETTES = {
        "fire": [
            (0.00, (  0,   0,   0)),
            (0.30, (200,   0,   0)),
            (0.60, (255, 140,   0)),
            (0.80, (255, 220,   0)),
            (1.00, (255, 255, 200)),
        ],
        "ocean": [
            (0.00, (  0,  10,  60)),
            (0.40, (  0, 120, 180)),
            (0.70, (  0, 190, 160)),
            (1.00, ( 80, 230, 255)),
        ],
        "neon": [
            (0.00, (  0, 255,   0)),
            (0.25, (255,   0, 255)),
            (0.50, (  0, 255, 255)),
            (0.75, (255, 255,   0)),
            (1.00, (  0, 255,   0)),
        ],
        "aurora": [
            (0.00, (  0,   5,  20)),
            (0.30, (  0, 180,  80)),
            (0.55, ( 20, 100, 200)),
            (0.80, (160,   0, 200)),
            (1.00, (  0,   5,  20)),
        ],
    }

    def __init__(
        self,
        n_leds       : int   = N_LEDS,
        fps          : int   = 44,
        gamma        : float = GAMMA,
        palette_name : str   = "aurora",
        brightness   : float = 0.25,
        speed        : float = 1.0,
        n_strips     : int   = 5,
        block_size   : int   = 150,
    ):
        self.n        = n_leds
        self.fps      = fps
        self.brightness = np.float32(brightness)
        self.speed    = np.float32(speed)

        # Strip / block topology (5 strips × 300 LEDs, blocks of 150 LEDs)
        self.n_strips      = n_strips
        self.leds_per_strip = n_leds // n_strips          # 300
        self.block_size    = block_size
        self.n_blocks      = n_leds // block_size         # 10

        # Gamma LUT (4096 stappen)
        self._gamma_lut = build_gamma_lut(gamma)

        # Actief palette
        self.set_palette(palette_name)

        # Herbruikbare canvas-buffer (geen heap-allocatie per frame)
        self._canvas  = np.zeros((n_leds, 3), dtype=np.float32)

        # Herbruikbare masker-buffers (pre-allocatie om GC te vermijden)
        self._block_mask = np.ones(n_leds, dtype=np.float32)
        self._strip_mask = np.ones((n_leds, 3), dtype=np.float32)

    def set_palette(self, name: str) -> None:
        stops = self.PALETTES.get(name, self.PALETTES["aurora"])
        self._palette_lut = make_palette_lut(stops)

    def render(
        self,
        t             : float,
        density       : float = 1.0,
        brightness_mul: float = 1.0,
        noise_rate    : float = 1.0,
        bpm           : float = 120.0,
        accent_dots   : "list[tuple] | None" = None,
        active_strips : "list[int] | None"   = None,
        mode          : str   = "noise",
        strip_params  : "list[dict] | None"  = None,
    ) -> np.ndarray:
        """Geef een (n, 3) uint8 frame terug voor tijdstip t (seconden).

        Parameters
        ----------
        strip_params : list of 5 dicts (one per strip), each with:
            offset    : float  — circular shift within the strip (LEDs)
            speed_mul : float  — per-strip noise speed multiplier (noise mode only)
            reverse   : bool   — flip pixel order within the strip
            mirror_of : int|None — copy+reverse another strip index into this one
        """
        import math as _math

        canvas = self._canvas
        canvas[:] = 0.0

        # Beat-synchrone tijdas
        beat_interval = 60.0 / max(float(bpm), 1.0)
        bar_s         = beat_interval * 4.0
        t_beat        = float(t) / bar_s
        speed_f       = float(self.speed)
        t_s           = t_beat * speed_f * float(noise_rate)
        beat_phase    = (float(t) / beat_interval) % 1.0

        # ── Canvas vullen op basis van mode ───────────────────────────────
        if mode == "interstitial_chase":
            self._fill_interstitial_chase(canvas, t, bpm, speed_f, beat_phase)
        elif mode == "vertical_pulse":
            self._fill_vertical_pulse(canvas, t, bpm, speed_f, beat_phase)
        elif mode == "mirror_symmetry":
            self._fill_mirror_symmetry(canvas, t_s)
        elif mode == "digital_rain":
            self._fill_digital_rain(canvas, t_s, speed_f)
        elif mode == "diamond_wave":
            self._fill_diamond_wave(canvas, t, speed_f, float(density))
        elif mode == "diamond_chase":
            self._fill_diamond_chase(canvas, t, speed_f, float(density))
        elif mode == "diamond_bloom":
            self._fill_diamond_bloom(canvas, t, bpm, speed_f, float(density))
        elif mode == "minimal_ambient":
            self._fill_minimal_ambient(canvas, t, speed_f, float(density))
        elif mode == "ember_drift":
            self._fill_ember_drift(canvas, t, speed_f, float(density))
        elif mode == "deep_void":
            self._fill_deep_void(canvas, t, speed_f, float(density))
        else:
            # ── mode="noise": per-strip noise met individuele speed + seed ─
            import math as _m
            beat_breath = np.float32(0.88 + 0.12 * _m.sin(beat_phase * 2.0 * _m.pi))
            lps = self.leds_per_strip
            for _s in range(self.n_strips):
                _lo = _s * lps
                _hi = min(self.n, _lo + lps)
                _lps = _hi - _lo
                _sp       = strip_params[_s] if strip_params else {}
                _spd      = float(_sp.get("speed_mul", 1.0)) if _sp else 1.0
                _t_s      = t_s * _spd
                _noise    = noise1d(_lps, _t_s, spatial_scale=0.005, time_scale=1.0,
                                    seed=_s)
                _base     = apply_palette(_noise, self._palette_lut)
                _intens   = (noise1d(_lps, _t_s, spatial_scale=0.012,
                                     time_scale=1.8, seed=10 + _s) ** 1.4)
                canvas[_lo:_hi] = _base * (_intens * beat_breath)[:, np.newaxis]

            # Block mask (alleen in noise-modus)
            if density < 0.98:
                seed = int(t_beat * 0.5)
                rs   = np.random.RandomState(seed)
                block_on = rs.choice(
                    [0.0, 1.0],
                    size=self.n_blocks,
                    p=[max(0.0, 1.0 - float(density)),
                       min(1.0, float(density))],
                )
                bm = self._block_mask
                expanded = np.repeat(block_on, self.block_size)
                end = self.n_blocks * self.block_size
                bm[:end] = expanded
                if end < self.n:
                    bm[end:] = 1.0
                canvas *= bm[:, np.newaxis]

        # ── Per-strip transforms: offset, reverse, mirror ─────────────────
        # Applied for ALL modes before masking so every effect gets diversity.
        if strip_params:
            self._apply_strip_transforms(canvas, strip_params)

        # ── Grouping/spacing comb-filter ──────────────────────────────────
        # When any strip_param has grouping > 1 or spacing > 0, apply a
        # repeating block mask: `grouping` lit LEDs then `spacing` dark LEDs.
        if strip_params:
            lps = self.leds_per_strip
            indices = np.arange(lps, dtype=np.int32)
            for s, sp in enumerate(strip_params):
                grp = int(sp.get("grouping", 1)) if sp else 1
                spc = int(sp.get("spacing",  0)) if sp else 0
                if grp < 1:
                    grp = 1
                if spc > 0 or grp > 1:
                    period = grp + spc
                    # Use strip's current offset to phase-shift the comb
                    off_i  = int(round(float(sp.get("offset", 0)))) % max(1, period)
                    mask   = ((indices - off_i) % period < grp).astype(np.float32)
                    lo = s * lps
                    hi = min(self.n, lo + lps)
                    canvas[lo:hi] *= mask[:hi - lo, np.newaxis]

        # ── Strip mask — actieve strips (geldt voor alle modes) ───────────
        if active_strips is not None:
            sm = self._strip_mask
            sm[:] = 0.0
            lps = self.leds_per_strip
            for s in active_strips:
                if 0 <= s < self.n_strips:
                    lo = s * lps
                    hi = min(self.n, lo + lps)
                    sm[lo:hi] = 1.0
            canvas *= sm

        # ── Accent subpixel-dots (top layer) ──────────────────────────────
        if accent_dots:
            for pos_f, rgb_01, sigma in accent_dots:
                col = np.array(rgb_01, dtype=np.float32)
                subpixel_dot(canvas, float(pos_f), col, sigma=float(sigma))

        # ── Helderheid + gamma ─────────────────────────────────────────────
        canvas *= np.float32(self.brightness) * np.float32(brightness_mul)
        corrected = apply_gamma_lut(np.clip(canvas, 0.0, 1.0), self._gamma_lut)
        return (corrected * 255.0).astype(np.uint8)

    def _apply_strip_transforms(
        self,
        canvas      : np.ndarray,
        strip_params: list,
    ) -> None:
        """Pas per-strip offset, reverse en mirror toe op de canvas in-place.

        Volgorde:
          Pass 1 — offset (circulaire verschuiving) + reverse per strip
          Pass 2 — mirror_of (kopieer + spiegeel een andere strip na pass 1)
        """
        lps = self.leds_per_strip

        # ── Pass 1: offset + reverse ───────────────────────────────────────
        for s, sp in enumerate(strip_params):
            if not sp:
                continue
            lo = s * lps
            hi = min(self.n, lo + lps)
            seg = canvas[lo:hi]

            if sp.get("reverse", False):
                seg = seg[::-1].copy()
            off = int(round(sp.get("offset", 0.0))) % lps
            if off:
                seg = np.roll(seg, off, axis=0)
            canvas[lo:hi] = seg

        # ── Pass 2: mirror (bron is al getransformeerd door pass 1) ───────
        for s, sp in enumerate(strip_params):
            if not sp:
                continue
            src = sp.get("mirror_of")
            if src is None or src == s or not (0 <= src < self.n_strips):
                continue
            lo_s, hi_s = src * lps, min(self.n, (src + 1) * lps)
            lo_d, hi_d = s   * lps, min(self.n, (s   + 1) * lps)
            n = min(hi_s - lo_s, hi_d - lo_d)
            canvas[lo_d : lo_d + n] = canvas[lo_s : lo_s + n][::-1]

    # ─────────────────────────────────────────────────────────────────────────
    # Club-mode canvas-fillers
    # ─────────────────────────────────────────────────────────────────────────

    def _fill_interstitial_chase(
        self,
        canvas     : np.ndarray,
        t          : float,
        bpm        : float,
        speed      : float,
        beat_phase : float,
    ) -> None:
        """Licht verspringt strip voor strip elke beat (1→2→3→4→5→1→…).

        Actieve strip: volledig opgelicht met noise-textuur + beat-flits.
        Vorige strip: 35% helderheid (trail).
        """
        import math as _m
        beat_interval = 60.0 / max(bpm, 1.0)
        lps = self.leds_per_strip

        beat_idx  = int(t / beat_interval)
        cur_strip = beat_idx % self.n_strips
        prv_strip = (beat_idx - 1) % self.n_strips

        # Beat-flits envelope: scherpe aanval bij beat-start, snel decay
        flash = _m.exp(-beat_phase * 4.0 * speed)

        # Noise-textuur voor actieve strip (1D simplex over 300 LEDs)
        t_s_local = float(t) * speed * 0.25
        noise_vals = noise1d(lps, t_s_local, spatial_scale=0.008, time_scale=1.0)
        colored    = apply_palette(noise_vals, self._palette_lut)   # (lps, 3)

        # Actieve strip
        lo = cur_strip * lps
        hi = min(self.n, lo + lps)
        canvas[lo:hi] = colored[:hi-lo] * (0.6 + 0.4 * flash)

        # Trail strip (35% van dezelfde textuur, fading uit)
        trail_amp = (1.0 - beat_phase) * 0.35
        if trail_amp > 0.01:
            lo2 = prv_strip * lps
            hi2 = min(self.n, lo2 + lps)
            canvas[lo2:hi2] = colored[:hi2-lo2] * trail_amp

    def _fill_vertical_pulse(
        self,
        canvas     : np.ndarray,
        t          : float,
        bpm        : float,
        speed      : float,
        beat_phase : float,   # unused — pulse is bar-synced
    ) -> None:
        """Alle strips flitsen tegelijk op de bar als een blinder.

        Scherpe aanval, exponentieel verval.  Kleur roteert per maat.
        """
        import math as _m
        beat_interval = 60.0 / max(bpm, 1.0)
        bar_s         = beat_interval * 4.0

        # Bar-phase: 0.0 = bar start, 1.0 = bar end
        bar_phase  = (float(t) % bar_s) / bar_s

        # Envelope: instant aan op bar-start, snel decay — blinder-gevoel
        flash = _m.exp(-bar_phase * 5.5 * max(speed, 0.5))

        # Kleur roteert per maat via palette-positie
        bar_idx    = int(t / bar_s)
        pal_pos    = (bar_idx * 0.25) % 1.0         # 4 kleuren per palette-cyclus
        pal_idx    = int(pal_pos * (len(self._palette_lut) - 1))
        base_color = self._palette_lut[pal_idx]     # (3,) float32

        # Subtiele spatiale variatie: intensiteits-golf over de hele strip
        arr   = np.arange(self.n, dtype=np.float32)
        wave  = 0.75 + 0.25 * np.sin(arr * 0.015 + float(t) * 2.0 * _m.pi * 0.5)

        canvas[:, 0] = base_color[0] * wave * flash
        canvas[:, 1] = base_color[1] * wave * flash
        canvas[:, 2] = base_color[2] * wave * flash

    def _fill_mirror_symmetry(
        self,
        canvas : np.ndarray,
        t_s    : float,
    ) -> None:
        """Strips 1 en 5 spiegelen strip 3 (center); strips 2 en 4 spiegelen.

        Topologie (index 0-4):
          Strip 0 (buiten links)  ← gespiegeld van strip 4 (buiten rechts)
          Strip 1 (midden links)  ← gespiegeld van strip 3 (midden rechts)
          Strip 2 (center)        → bron

        De buitenste strips krijgen 60% amplitude; middelste 85%.
        """
        lps = self.leds_per_strip

        # Genereer bron-noise voor 300 LEDs (center strip)
        noise_vals = noise1d(lps, t_s, spatial_scale=0.006, time_scale=1.0)
        center     = apply_palette(noise_vals, self._palette_lut)   # (lps, 3)
        center_rev = center[::-1]                                    # gespiegeld

        # Strip 2: center (bron)
        canvas[2 * lps : 3 * lps] = center

        # Strips 1 en 3: gespiegeld paar (85%)
        canvas[1 * lps : 2 * lps] = center_rev * np.float32(0.85)
        canvas[3 * lps : 4 * lps] = center     * np.float32(0.85)

        # Strips 0 en 4: tweede gespiegeld paar (60%), lichte tijdsverschuiving
        noise_outer = noise1d(lps, t_s + 7.3, spatial_scale=0.006, time_scale=1.0)
        outer       = apply_palette(noise_outer, self._palette_lut)
        canvas[0 * lps : 1 * lps] = outer[::-1] * np.float32(0.60)
        canvas[4 * lps : 5 * lps] = outer        * np.float32(0.60)

    def _fill_digital_rain(
        self,
        canvas : np.ndarray,
        t_s    : float,
        speed  : float,
    ) -> None:
        """Subpixel-druppels dalen neer over alle 5 strips, elk met eigen snelheid.

        3 druppels per strip × 5 strips = 15 actieve punten.
        Elke druppel heeft een helder hoofd + vervagende staart.
        """
        lps = self.leds_per_strip

        # Vaste parameters per druppel (deterministisch op basis van strip/drop-idx)
        # speed_mults: droegen op strip 0 langzamer, strip 4 sneller
        _drop_speeds   = [0.18, 0.31, 0.47]   # relatieve snelheid per druppel
        _drop_phases   = [0.00, 0.37, 0.71]   # start-offset als fractie van strip
        _strip_offsets = [1.00, 1.20, 1.45, 1.65, 1.90]  # strip-snelheid multiplier

        for s in range(self.n_strips):
            strip_lo  = s * lps
            strip_mul = _strip_offsets[s]

            for d in range(3):
                drop_speed = _drop_speeds[d] * strip_mul * max(speed, 0.3)
                phase      = _drop_phases[d]

                # Positie binnen de strip (0..lps-1), wraps
                pos_frac = (t_s * drop_speed + phase) % 1.0
                pos_led  = pos_frac * (lps - 1)
                abs_pos  = strip_lo + pos_led

                # Kleur: palette op basis van druppel-index + strip
                pal_frac = (s * 0.2 + d * 0.13) % 1.0
                pal_idx  = int(pal_frac * (len(self._palette_lut) - 1))
                head_col = self._palette_lut[pal_idx]   # (3,) float32

                # Hoofd: heldere dot, sigma=1.2
                subpixel_dot(canvas, abs_pos, head_col, sigma=1.2)

                # Staart: 4 dots achter het hoofd, kwadratisch dimmend
                for i in range(1, 5):
                    tail_pos = abs_pos - i * 2.5
                    if tail_pos < strip_lo:
                        break
                    amp = (0.55 ** i)
                    tail_col = head_col * np.float32(amp)
                    subpixel_dot(canvas, tail_pos, tail_col, sigma=1.5)

    # ── Diamond Wave family ───────────────────────────────────────────────────

    def _fill_diamond_wave(
        self,
        canvas  : np.ndarray,
        t       : float,
        speed   : float,
        density : float,
    ) -> None:
        """Rolling diamond wave: target half-lengths [100,300,600,900,1200]
        cycle across the 5 strips.  Each strip grows from its centre (LED 150)
        outward.  Density controls edge-fade width.

        Normalised to half-strip space:
            100 → 12.5 LEDs   (narrow sliver)
            300 → 37.5 LEDs
            600 → 75.0 LEDs   (half of half-strip)
            900 → 112.5 LEDs
           1200 → 150.0 LEDs  (full strip)
        """
        lps    = self.leds_per_strip          # 300
        centre = np.float32(lps * 0.5)        # 150.0

        # Target half-lengths in LEDs (normalised to 0–150)
        TARGETS = np.array([100, 300, 600, 900, 1200], dtype=np.float32) / 1200.0 * 150.0

        # Continuous fractional shift: 0→5 over one full cycle
        shift = (t * float(speed) * 0.4) % 5.0

        dist = np.abs(np.arange(lps, dtype=np.float32) - centre)  # (lps,)

        for s in range(5):
            lo = s * lps

            # Smoothly interpolate target half-length for this strip
            idx_f  = (s + shift) % 5.0
            i_lo   = int(idx_f) % 5
            i_hi   = (i_lo + 1) % 5
            frac   = idx_f - int(idx_f)
            half   = float(TARGETS[i_lo]) + frac * float(TARGETS[i_hi] - TARGETS[i_lo])

            # Soft edge: fade_width grows with density
            fade_w   = max(2.0, half * (density * 0.5 + 0.05))
            envelope = np.clip((half - dist) / fade_w, 0.0, 1.0)   # (lps,)

            # Palette: hue follows wave phase; spatial variation from centre to edge
            pal_base = ((s + shift) / 5.0) % 1.0
            pal_vals = np.clip(
                np.float32(pal_base) * 0.6 + dist / 150.0 * 0.4,
                0.0, 1.0,
            )
            colored = apply_palette(pal_vals, self._palette_lut)    # (lps, 3)
            canvas[lo : lo + lps] = colored * envelope[:, np.newaxis]

    def _fill_diamond_chase(
        self,
        canvas  : np.ndarray,
        t       : float,
        speed   : float,
        density : float,
    ) -> None:
        """Single diamond peak chases through the 5 strips.

        The peak strip is fully open (half=150 LEDs), neighbours taper off
        with a Gaussian in strip-space.  density widens the Gaussian falloff
        so at high density all strips glow; at low density only the peak strip.
        """
        lps    = self.leds_per_strip
        centre = np.float32(lps * 0.5)

        dist = np.abs(np.arange(lps, dtype=np.float32) - centre)   # (lps,)

        # Float strip-index of the travelling peak (wraps 0–5)
        peak = (t * float(speed) * 0.55) % 5.0

        # Gaussian sigma in strip-space: density 0→tight, 1→wide
        sigma_strip = max(0.3, float(density) * 2.2 + 0.3)

        for s in range(5):
            lo = s * lps

            # Wrap-aware strip distance from peak
            raw_d = abs(s - peak)
            strip_d = min(raw_d, 5.0 - raw_d)
            strip_w = float(np.exp(-0.5 * (strip_d / sigma_strip) ** 2))

            half     = 150.0 * strip_w
            fade_w   = max(2.0, half * 0.25)
            envelope = np.clip((half - dist) / max(fade_w, 0.1), 0.0, 1.0)

            # Hue rotates with peak position + per-strip offset
            pal_frac = ((peak / 5.0) + s * 0.2) % 1.0
            # Centre pixels slightly brighter hue variant
            pal_vals = np.clip(
                np.float32(pal_frac) + (1.0 - dist / 150.0) * 0.25,
                0.0, 1.0,
            )
            colored = apply_palette(pal_vals, self._palette_lut)
            canvas[lo : lo + lps] = colored * (envelope * np.float32(strip_w))[:, np.newaxis]

    def _fill_diamond_bloom(
        self,
        canvas  : np.ndarray,
        t       : float,
        bpm     : float,
        speed   : float,
        density : float,
    ) -> None:
        """Crown/bloom: all 5 strips expand from centre simultaneously but with
        staggered phases.  Outer strips lead; centre strip lags — producing a
        crown-opening silhouette on every bar.

        Phase offsets (strip index 0–4, centre=2):
            [+0.40, +0.20, 0.00, +0.20, +0.40]
        """
        import math as _m

        lps           = self.leds_per_strip
        centre        = np.float32(lps * 0.5)
        beat_interval = 60.0 / max(float(bpm), 1.0)

        dist = np.abs(np.arange(lps, dtype=np.float32) - centre)

        # Phase offsets: outer strips lead by up to 0.40 of a cycle
        OFFSETS = (0.40, 0.20, 0.00, 0.20, 0.40)

        # Colour symmetry: outer strips get pal_pos 0.0 (warm), centre 0.65 (cool)
        # Slowly drifts over time
        drift = (t * float(speed) * 0.015) % 1.0
        PAL_BASES = tuple(
            (abs(s - 2) / 4.0 * 0.65 + drift) % 1.0 for s in range(5)
        )

        for s in range(5):
            lo    = s * lps
            phase = ((t * float(speed) / beat_interval) + OFFSETS[s]) % 1.0

            # Sinusoidal half-length: 8 LEDs → 148 LEDs (leaves a 2-LED dark edge)
            half     = 8.0 + 140.0 * (0.5 + 0.5 * _m.sin(phase * 2.0 * _m.pi))
            fade_w   = max(2.0, half * (float(density) * 0.45 + 0.04))
            envelope = np.clip((half - dist) / fade_w, 0.0, 1.0)

            pal_vals = np.full(lps, np.float32(PAL_BASES[s]), dtype=np.float32)
            colored  = apply_palette(pal_vals, self._palette_lut)
            canvas[lo : lo + lps] = colored * envelope[:, np.newaxis]

    # ── Minimal Ambient family (down / bridge phrases) ────────────────────────

    def _fill_minimal_ambient(
        self,
        canvas  : np.ndarray,
        t       : float,
        speed   : float,
        density : float,
    ) -> None:
        """Slow strip-roulette for down/bridge phrases.

        Algorithm
        ---------
        1. A virtual strip position oscillates sinusoidally over [0, 4].
           Smoothstep on the fractional part extends the "dwell" at each
           integer position, so a strip stays fully lit for ~4–8 bars before
           softly crossing to the next.  At most 2 strips are ever active.

        2. Each active strip receives a very-slow Simplex noise texture
           (noise_rate ≈ 0.01–0.02).  The noise is raised to power 1.5 so
           bright peaks stay bright but dark areas deepen → "drifting mist".

        3. A Gaussian envelope centred at LED 150 attenuates every strip:
           σ ≈ 22 % of strip length so edge pixels decay to < 1 % brightness.
           No hard edge anywhere on the strip.

        4. density [0–1] controls σ: higher density → wider lit region.

        Back strip (not in this canvas): left 0 by caller — CaveStripRenderer
        is responsible for the 768-LED back strip and must be OFF for down/bridge.
        """
        import math as _m

        lps = self.leds_per_strip  # 300

        # ── Virtual strip position (smooth-dwell oscillation) ────────────────
        # Period = 60 / speed seconds.  At speed=0.5 → 120s full cycle.
        # Each strip visited for ~120/4 = 30s ≈ 7–8 bars at 125 BPM.
        period  = 60.0 / max(float(speed), 0.05)
        virt_raw = 2.0 + 2.0 * _m.sin(2.0 * _m.pi * t / period)  # [0, 4]

        i_lo  = int(virt_raw)
        frac  = virt_raw - i_lo
        # Smoothstep: f(x) = 3x²−2x³  → slow at endpoints, fast at midpoint
        # → strip dwells at integer position (1 strip fully lit) longer
        frac_s = frac * frac * (3.0 - 2.0 * frac)

        lo_strip = i_lo % 5
        hi_strip = (i_lo + 1) % 5
        lo_w = np.float32(1.0 - frac_s)
        hi_w = np.float32(frac_s)

        # ── Gaussian centre envelope ─────────────────────────────────────────
        # sigma scales with density: 0.18 (tight, lo density) → 0.30 (wide)
        pos   = np.arange(lps, dtype=np.float32)
        sigma = np.float32(lps * (0.18 + float(density) * 0.12))
        gauss = np.exp(
            -0.5 * ((pos - np.float32(lps * 0.5)) / sigma) ** 2,
            dtype=np.float32,
        )

        # ── Very-slow mist noise ─────────────────────────────────────────────
        # time axis scaled so a full noise cycle takes ~60–120 s at speed=0.5
        t_mist = float(t) * max(float(speed), 0.05) * 0.008

        for s_idx, w in ((lo_strip, lo_w), (hi_strip, hi_w)):
            if w < 0.005:
                continue
            lo = s_idx * lps

            # Unique seed per strip so strips don't phase-lock
            raw_n = noise1d(lps, t_mist + s_idx * 19.7,
                            spatial_scale=0.007, time_scale=1.0,
                            seed=30 + s_idx)
            # Power-curve: deepen dark zones → smoke/mist look
            mist  = np.power(raw_n, np.float32(1.5))
            color = apply_palette(mist, self._palette_lut)         # (lps, 3)

            canvas[lo : lo + lps] += color * (gauss * w)[:, np.newaxis]

    def _fill_ember_drift(
        self,
        canvas  : np.ndarray,
        t       : float,
        speed   : float,
        density : float,
    ) -> None:
        """Warm ember-drift variant of minimal_ambient.

        Differences from minimal_ambient:
        - Two independent slow noise layers per strip (base + drift) blended at
          ratio 0.7/0.3 → organic depth variation.
        - Each active strip has a second "ember glow" at a slightly different
          temporal offset (+37 s) so the two noise layers breathe independently.
        - Gaussian σ is wider (0.27–0.38) for a softer, more enveloping glow.
        - Strip oscillation uses cos instead of sin so it starts mid-ramp,
          giving a different initial feel in a set.
        """
        import math as _m

        lps    = self.leds_per_strip
        period = 55.0 / max(float(speed), 0.05)
        virt_raw = 2.0 + 2.0 * _m.cos(2.0 * _m.pi * t / period)  # [0, 4]

        i_lo  = int(virt_raw)
        frac  = virt_raw - i_lo
        frac_s = frac * frac * (3.0 - 2.0 * frac)

        lo_strip = i_lo % 5
        hi_strip = (i_lo + 1) % 5

        pos   = np.arange(lps, dtype=np.float32)
        sigma = np.float32(lps * (0.27 + float(density) * 0.11))
        gauss = np.exp(
            -0.5 * ((pos - np.float32(lps * 0.5)) / sigma) ** 2,
            dtype=np.float32,
        )

        t_base = float(t) * max(float(speed), 0.05) * 0.006

        for s_idx, raw_w in ((lo_strip, 1.0 - frac_s), (hi_strip, frac_s)):
            w = np.float32(raw_w)
            if w < 0.004:
                continue
            lo = s_idx * lps

            # Base layer: deep slow pulse
            n_base  = noise1d(lps, t_base + s_idx * 13.1,
                              spatial_scale=0.005, time_scale=1.0, seed=40 + s_idx)
            # Drift layer: slightly faster offset → independent breathing
            n_drift = noise1d(lps, t_base + 37.0 + s_idx * 7.3,
                              spatial_scale=0.009, time_scale=1.0, seed=50 + s_idx)

            mist  = np.power(n_base * 0.7 + n_drift * 0.3, np.float32(1.3))
            color = apply_palette(mist, self._palette_lut)

            canvas[lo : lo + lps] += color * (gauss * w)[:, np.newaxis]

    def _fill_deep_void(
        self,
        canvas  : np.ndarray,
        t       : float,
        speed   : float,
        density : float,
    ) -> None:
        """Dark minimal variant — single strip lit at a time, no crossfade.

        The active strip fades in/out using a slow breathe envelope (sin²).
        When the strip is dark, canvas is fully black → true negative space.
        Gaussian σ is very narrow (0.14–0.22) leaving outer LEDs near black.
        """
        import math as _m

        lps    = self.leds_per_strip
        period = 50.0 / max(float(speed), 0.05)

        # Which strip is active (hard-quantized to single integer)
        virt_raw  = 2.0 + 2.0 * _m.sin(2.0 * _m.pi * t / period)
        cur_strip = round(virt_raw) % 5

        # Breathe: strip pulses in/out on a slow sine; never fully off inside pulse
        breathe_period = period / 5.0
        breathe_phase  = (t % breathe_period) / breathe_period
        breathe = 0.35 + 0.65 * (_m.sin(breathe_phase * _m.pi)) ** 2

        pos   = np.arange(lps, dtype=np.float32)
        sigma = np.float32(lps * (0.14 + float(density) * 0.08))
        gauss = np.exp(
            -0.5 * ((pos - np.float32(lps * 0.5)) / sigma) ** 2,
            dtype=np.float32,
        )

        lo     = cur_strip * lps
        t_slow = float(t) * max(float(speed), 0.05) * 0.005
        raw_n  = noise1d(lps, t_slow + cur_strip * 11.9,
                         spatial_scale=0.006, time_scale=1.0, seed=60 + cur_strip)
        mist   = np.power(raw_n, np.float32(1.8))   # very deep mist
        color  = apply_palette(mist, self._palette_lut)

        canvas[lo : lo + lps] = color * (gauss * np.float32(breathe))[:, np.newaxis]

    # ── Public standalone methods (state-duck-typed) ──────────────────────────

    def render_minimal_ambient(self, t: float, state) -> np.ndarray:
        """Render minimal ambient frame from a state object.

        state: .speed (float, ~0.4), .brightness (float, ~0.2), .density (float)

        Returns (1500, 3) uint8.
        The 5 strips contain the ambient render; the 768-LED back strip is a
        separate device rendered by CaveStripRenderer — it must stay dark for
        down/bridge phrases (caller must skip CaveStripRenderer for those phrases).
        """
        canvas = self._canvas
        canvas[:] = 0.0
        self._fill_minimal_ambient(
            canvas,
            t       = float(t),
            speed   = float(getattr(state, "speed",    0.4)),
            density = float(getattr(state, "density",  0.4)),
        )
        br = np.float32(self.brightness) * np.float32(getattr(state, "brightness", 0.2))
        canvas *= br
        corrected = apply_gamma_lut(np.clip(canvas, 0.0, 1.0), self._gamma_lut)
        return (corrected * 255.0).astype(np.uint8)

    # ── Public standalone method (state-duck-typed) ───────────────────────────

    def render_dynamic_diamond_wave(self, t: float, state) -> np.ndarray:
        """Render a single diamond-wave frame from a state object.

        state must expose: .speed (float), .brightness (float), .density (float)

        Returns (1500, 3) uint8.  Wraps _fill_diamond_wave with brightness +
        gamma correction applied.
        """
        canvas = self._canvas
        canvas[:] = 0.0
        self._fill_diamond_wave(
            canvas,
            t        = float(t),
            speed    = float(getattr(state, "speed",    1.0)),
            density  = float(getattr(state, "density",  0.5)),
        )
        br = np.float32(self.brightness) * np.float32(getattr(state, "brightness", 1.0))
        canvas *= br
        corrected = apply_gamma_lut(np.clip(canvas, 0.0, 1.0), self._gamma_lut)
        return (corrected * 255.0).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone demo (zonder WLED-hardware)
# ─────────────────────────────────────────────────────────────────────────────

def _benchmark(n_leds: int = 1500, n_frames: int = 100) -> None:
    """Meet render-tijd per frame en drempelwaarden voor 44 fps."""
    engine = AnimationEngine(n_leds=n_leds)
    t0     = time.perf_counter()

    for i in range(n_frames):
        t      = i / 44.0
        frame  = engine.render(t)

    elapsed  = time.perf_counter() - t0
    per_frame = elapsed / n_frames * 1000
    budget    = 1000 / 44

    print(f"n_leds      : {n_leds}")
    print(f"frames      : {n_frames}")
    print(f"totaal      : {elapsed:.3f} s")
    print(f"per frame   : {per_frame:.2f} ms  (budget 44 fps = {budget:.1f} ms)")
    print(f"max FPS     : {1000 / per_frame:.0f}")
    print(f"frame[0]    : {frame[0]}  (uint8 RGB van led 0)")


if __name__ == "__main__":
    _benchmark()
