"""Vault strip generative renderer — 2D effect library.

5×300 snake-wired rig (1500 LEDs).
All patterns operate on logical (row 0–4, col 0–299) coordinates.
render() returns (1500, 3) uint8 in physical strip order.

Design rule: NO full-strip fills. Every pattern uses individual LEDs
or tight clusters — sparks, dots, thin lines, small blobs.
"""

import math
import numpy as np

_N_STRIPS   = 5
_LEDS_STRIP = 300
_N_LEDS     = _N_STRIPS * _LEDS_STRIP   # 1500
_TAU        = math.tau
_PI         = math.pi


# ── Grid precompute ───────────────────────────────────────────────────────────

def _precompute_grid():
    row_idx = np.arange(_N_LEDS, dtype=np.int32) // _LEDS_STRIP
    raw_col = np.arange(_N_LEDS, dtype=np.int32) %  _LEDS_STRIP
    log_col = np.where(row_idx % 2 == 1, _LEDS_STRIP - 1 - raw_col, raw_col)
    x = log_col.astype(np.float32) / (_LEDS_STRIP - 1)
    y = row_idx.astype(np.float32) / (_N_STRIPS   - 1)
    logical_idx     = row_idx * _LEDS_STRIP + log_col
    logical_to_phys = np.argsort(logical_idx)
    return row_idx, log_col, x, y, logical_to_phys

_ROW, _COL, _X, _Y, _LOG_TO_PHYS = _precompute_grid()

# Per-LED / per-column deterministic random phases (seeded, never change)
_rng         = np.random.default_rng(7331)
_LED_R0      = _rng.random(_N_LEDS).astype(np.float32)   # phase A per LED
_LED_R1      = _rng.random(_N_LEDS).astype(np.float32)   # phase B per LED
_COL_R0      = _rng.random(_LEDS_STRIP).astype(np.float32)  # phase per column
_COL_R1      = _rng.random(_LEDS_STRIP).astype(np.float32)

# Aspect-corrected canvas centre distance (for radial patterns)
# Row range = 4px physical, col range = 299px — scale rows to feel square-ish
_CX = _LEDS_STRIP / 2.0          # logical col centre  = 150
_CY = (_N_STRIPS - 1) / 2.0      # logical row centre  = 2.0
_ASPECT = _LEDS_STRIP / _N_STRIPS  # ~60 — scale row distances
_DIST_C = np.sqrt((_COL - _CX)**2 + ((_ROW - _CY) * _ASPECT)**2).astype(np.float32)
_ANGLE_C = np.arctan2((_ROW - _CY) * _ASPECT, (_COL - _CX)).astype(np.float32)
_MAX_DIST = float(np.max(_DIST_C))


# ── Color helpers ─────────────────────────────────────────────────────────────

def _hue_rgb(hue):
    h6 = np.asarray(hue, dtype=np.float32) * 6.0
    r  = np.clip(np.abs(h6 - 3.0) - 1.0, 0.0, 1.0) * 255
    g  = np.clip(2.0 - np.abs(h6 - 2.0), 0.0, 1.0) * 255
    b  = np.clip(2.0 - np.abs(h6 - 4.0), 0.0, 1.0) * 255
    return r, g, b

def _lerp_color(c1, c2, t):
    if np.ndim(t) == 0:
        return c2 + t * (c1 - c2)
    t3 = np.clip(t, 0, 1)[:, np.newaxis]
    return c2[np.newaxis, :] + t3 * (c1 - c2)[np.newaxis, :]


# ── Renderer ──────────────────────────────────────────────────────────────────

class VaultRenderer:

    def __init__(self):
        self._n   = _N_LEDS
        self._row = _ROW
        self._col = _COL
        self._x   = _X
        self._y   = _Y
        self._l2p = _LOG_TO_PHYS

    def render(self, t: float, bpm: float, params: dict,
               beat_phase: float = 0.0) -> np.ndarray:
        speed   = float(params.get("speed",      1.0))
        bright  = float(params.get("brightness", 0.7))
        cov     = float(params.get("coverage",   0.7))
        size    = float(params.get("size",       0.25))
        pattern = params.get("pattern", "ping_pong")
        sync    = params.get("beat_sync", "off")

        c1 = np.array(params.get("color_primary",   [220, 80, 10]),  dtype=np.float32)
        c2 = np.array(params.get("color_secondary", [ 10, 10, 60]),  dtype=np.float32)

        beats = t * bpm / 60.0

        pulse = 1.0
        if sync == "beat":
            pulse = 0.55 + 0.45 * math.cos(beat_phase * _TAU)
        elif sync == "2beat":
            pulse = 0.55 + 0.45 * math.cos(beat_phase * _PI)
        elif sync == "bar":
            bar_phase = (beats % 4) / 4.0
            pulse = 0.6 + 0.4 * math.cos(bar_phase * _TAU)

        result = self._dispatch(pattern, t, beats, speed, size,
                                beat_phase, bpm, c1, c2, cov)

        if result.ndim == 1:
            threshold = 1.0 - cov
            intensity = np.where(result > threshold, result,
                                 result * 0.05).astype(np.float32)
            intensity *= float(pulse)
            pixels = _lerp_color(c1, c2, intensity)
            pixels *= bright
        else:
            pixels = result.astype(np.float32) * bright * float(pulse)

        pixels_phys = pixels[self._l2p]
        return np.clip(pixels_phys, 0, 255).astype(np.uint8)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, name, t, beats, speed, size, bp, bpm, c1, c2, cov):
        row = self._row.astype(np.float32)
        col = self._col.astype(np.float32)
        x   = self._x
        y   = self._y

        # ── matrix_rain ───────────────────────────────────────────────────
        if name == "matrix_rain":
            # Individual drops falling column-by-column, sparse — like The Matrix.
            # Each column drops at its own speed; only ~50-70% of columns are active.
            density   = max(0.3, cov * 0.75)
            cidx      = self._col  # (1500,) int32 col index

            # Per-column active flag and speed variation
            col_active = (_COL_R0[cidx] < density).astype(np.float32)
            col_speed  = 0.5 + _COL_R1[cidx] * 1.2     # 0.5–1.7× variation

            # Drop y position cycles from –1 to ROWS+1 (rows above / below canvas)
            cycle     = _N_STRIPS + 2.5
            drop_y    = (beats * speed * 0.7 * col_speed
                         + _COL_R0[cidx] * cycle) % cycle - 0.5

            # How far the drop head is above this LED's row
            dist      = drop_y - row                    # positive = head below/at LED
            trail     = 2.2
            in_trail  = col_active * np.clip(1.0 - dist / trail, 0, 1) * (dist >= 0) * (dist < trail)
            # Head = bright c1, tail fades to c2
            head_w    = np.exp(-dist * 1.4).astype(np.float32) * (dist >= 0) * (dist < trail)
            rgb       = (in_trail[:, None] *
                         (head_w[:, None] * c1[None, :] +
                          (1 - head_w)[:, None] * c2[None, :] * 0.4))
            return np.clip(rgb, 0, 255)

        # ── sparks ────────────────────────────────────────────────────────
        elif name == "sparks":
            # Random individual LED sparks — embers, never full strips.
            # Two independent staggered spark layers for richness.
            spark_dur = max(0.06, 0.25 / speed)
            interval  = spark_dur * max(3.0, 15.0 * (1.0 - cov + 0.1))

            ts0 = (t + _LED_R0 * interval) % interval
            ts1 = (t + _LED_R1 * interval + interval * 0.5) % interval

            fade0 = np.where(ts0 < spark_dur,
                             np.exp(-ts0 / spark_dur * 3.0), 0.0).astype(np.float32)
            fade1 = np.where(ts1 < spark_dur,
                             np.exp(-ts1 / spark_dur * 3.0) * 0.6, 0.0).astype(np.float32)

            intensity = np.clip(fade0 + fade1, 0, 1)
            # Random hue-shift per LED for fire/ember variety
            shift = _LED_R0 * 0.15
            rgb   = (intensity[:, None] *
                     (c1[None, :] * (1.0 - shift[:, None]) +
                      c2[None, :] * shift[:, None]))
            return np.clip(rgb, 0, 255)

        # ── ping_pong ─────────────────────────────────────────────────────
        elif name == "ping_pong":
            # Small bright blobs bouncing freely in 2D like billiard balls.
            # Each blob is a tight gaussian — never fills a strip.
            n_balls = 5
            rgb     = np.zeros((_N_LEDS, 3), dtype=np.float32)
            sx      = 4.5        # gaussian sigma in x (pixels)
            sy      = 0.5        # gaussian sigma in y (rows) — VERY tight

            for i in range(n_balls):
                ph   = i * 0.618034       # golden ratio offset
                # x: triangle wave  0 → COLS-1 → 0
                vx   = speed * (0.25 + i * 0.07)
                tx   = (t * vx + ph) % 2.0
                bx   = tx * (_LEDS_STRIP - 1) if tx < 1.0 else (2.0 - tx) * (_LEDS_STRIP - 1)
                # y: triangle wave  0 → ROWS-1 → 0  (different freq → avoids sync)
                vy   = speed * (0.13 + i * 0.05)
                ty   = (t * vy + ph * 1.37) % 2.0
                by   = ty * (_N_STRIPS - 1) if ty < 1.0 else (2.0 - ty) * (_N_STRIPS - 1)

                g    = np.exp(-((col - bx)**2 / (2 * sx**2) +
                                (row - by)**2 / (2 * sy**2))).astype(np.float32)
                clr  = c1 if i % 2 == 0 else c2
                rgb += g[:, None] * clr[None, :]

            return np.clip(rgb, 0, 255)

        # ── warp ──────────────────────────────────────────────────────────
        elif name == "warp":
            # Classic warp-speed starfield. Stars stream outward from canvas
            # centre. Each LED acts as a star with a unique phase offset so
            # the grid twinkles with individual points, never filling strips.
            n_pulses = 4
            streak_w = _MAX_DIST * max(0.03, size * 0.18)

            # Per-LED phase variation — each star has a slight timing offset
            phase_var = _LED_R0 * _MAX_DIST * 0.35

            pulse_period = _MAX_DIST / (speed * 80.0)   # seconds per pulse
            total = np.zeros(_N_LEDS, dtype=np.float32)

            for p in range(n_pulses):
                front = ((t + p * pulse_period / n_pulses + phase_var)
                         % pulse_period) / pulse_period * _MAX_DIST
                diff  = np.abs(_DIST_C - front)
                total += np.exp(-(diff**2) / (2 * streak_w**2)).astype(np.float32)

            # Bright in centre, and vary colour by angle
            hue    = (_ANGLE_C / _TAU + 0.5) % 1.0
            hr, hg, hb = _hue_rgb(hue)
            rgb    = (np.stack([hr, hg, hb], axis=1)
                      * np.clip(total, 0, 1)[:, None])
            # Blend with c1/c2 based on direction
            blend  = (np.sin(_ANGLE_C) * 0.5 + 0.5).astype(np.float32)
            colour = _lerp_color(c1, c2, blend)
            rgb    = rgb * 0.4 + colour * np.clip(total, 0, 1)[:, None] * 0.6
            return np.clip(rgb, 0, 255)

        # ── meteor_shower ─────────────────────────────────────────────────
        elif name == "meteor_shower":
            # Small meteors with bright heads and decaying tails flying
            # diagonally across the canvas.  Each stays in its "lane"
            # (a fractional row) so they never fill whole strips.
            n_met = 6
            rgb   = np.zeros((_N_LEDS, 3), dtype=np.float32)
            tail  = 22.0 + size * 30

            for i in range(n_met):
                ph     = i * 0.382034
                period = (1.8 + i * 0.22) / speed
                frac   = (t / period + ph) % 1.0

                # Head x: sweeps full width + overshoot
                hx = frac * (_LEDS_STRIP + 30) - 15
                # Lane y: fractional row — never sits on a full strip
                hy = (i * 0.618 % 1.0) * (_N_STRIPS - 1)
                # Slight vertical drift during pass
                hy = hy + math.sin(t * 0.4 + i) * 0.6

                # Direction: mostly +x with small diagonal
                ang    = (i % 3 - 1) * 0.25    # –0.25, 0, or +0.25 rad
                dx_dir = math.cos(ang)
                dy_dir = math.sin(ang)

                # Project each LED onto the meteor axis
                dcx  = col - hx
                dcy  = row - hy
                proj = dcx * dx_dir + dcy * dy_dir    # along trail
                perp = dcx * (-dy_dir) + dcy * dx_dir  # perpendicular

                in_t  = (proj < 0) & (proj > -tail) & (np.abs(perp) < 0.65)
                bright = np.where(in_t, np.exp(proj / (tail * 0.35)), 0.0)
                clr   = c1 if i % 2 == 0 else c2
                rgb  += bright[:, None].astype(np.float32) * clr[None, :]

            return np.clip(rgb, 0, 255)

        # ── fireflies ─────────────────────────────────────────────────────
        elif name == "fireflies":
            # Individual soft-glowing points wandering on organic Lissajous
            # paths.  Each firefly is 1–2 LEDs wide, blinks at its own rate.
            n_ff = 10
            rgb  = np.zeros((_N_LEDS, 3), dtype=np.float32)
            sx   = 3.5 + size * 8
            sy   = 0.5

            for i in range(n_ff):
                ph  = i * 2.399963    # golden angle * i
                wx  = speed * (0.08 + i * 0.012)
                wy  = speed * (0.06 + i * 0.009)
                fx  = (math.sin(_TAU * wx * t + ph) + 1.0) * 0.5 * (_LEDS_STRIP - 1)
                fy  = (math.sin(_TAU * wy * t      ) + 1.0) * 0.5 * (_N_STRIPS - 1)
                # Blink
                bl  = (math.sin(_TAU * speed * (0.3 + i * 0.07) * t + i) * 0.5 + 0.5)
                g   = (np.exp(-((col - fx)**2 / (2*sx**2) +
                                (row - fy)**2 / (2*sy**2))).astype(np.float32) * bl)
                clr = c1 if i % 2 == 0 else c2
                rgb += g[:, None] * clr[None, :]

            return np.clip(rgb, 0, 255)

        # ── dna_helix ─────────────────────────────────────────────────────
        elif name == "dna_helix":
            # Two sine-wave strands weaving through all 5 rows, connected by
            # rungs.  THIN lines — sigma ~0.45 rows.
            freq    = 2.0 + size * 6.0
            phase   = t * speed * 0.4
            sigma   = 0.45

            # Strands in logical row coords
            sa = (np.sin(x * freq * _TAU + phase        ) * 0.5 + 0.5) * (_N_STRIPS - 1)
            sb = (np.sin(x * freq * _TAU + phase + _PI  ) * 0.5 + 0.5) * (_N_STRIPS - 1)

            ga = np.exp(-((y * (_N_STRIPS-1) - sa)**2) / (2*sigma**2)).astype(np.float32)
            gb = np.exp(-((y * (_N_STRIPS-1) - sb)**2) / (2*sigma**2)).astype(np.float32)

            # Rungs: short vertical connectors spaced every ~25 cols
            rung_w   = 0.015
            cx_phase = (x * freq + phase / _TAU) % 1.0
            rung_cen = 0.5          # rungs at half-period (where strands cross)
            rung_g   = np.exp(-((cx_phase - rung_cen)**2) / (2*rung_w**2)).astype(np.float32)
            mid_y    = (sa + sb) * 0.5
            rung_b   = np.exp(-((y * (_N_STRIPS-1) - mid_y)**2) / (2*1.2**2)).astype(np.float32)
            rung     = rung_g * rung_b * 0.5

            rgb = (ga[:, None] * c1[None, :] +
                   gb[:, None] * c2[None, :] +
                   rung[:, None] * (c1[None, :] + c2[None, :]) * 0.4)
            return np.clip(rgb, 0, 255)

        # ── raindrops ─────────────────────────────────────────────────────
        elif name == "raindrops":
            # Individual drops fall from row 0 to row 4, leaving a short
            # glowing trail.  Each column activates at its own time.
            density  = max(0.2, cov * 0.8)
            cidx     = self._col

            col_on   = (_COL_R0[cidx] < density).astype(np.float32)
            # Stagger drop timing by column
            cycle    = (_N_STRIPS + 2.0) / (speed * 1.4)
            offset   = _COL_R1[cidx] * cycle
            drop_y   = (t + offset) % cycle / cycle * (_N_STRIPS + 1.5) - 0.75

            dist     = drop_y - row     # +ve when head is below this LED
            trail    = 1.8
            in_t     = col_on * (dist >= 0).astype(np.float32) * (dist < trail).astype(np.float32)
            fade     = np.exp(-dist * 2.2).astype(np.float32) * in_t

            # Tail colour c2, head c1
            head_w   = np.exp(-dist * 4.0).astype(np.float32) * in_t
            rgb      = (fade[:, None] *
                        (head_w[:, None] * c1[None, :] +
                         (1 - head_w)[:, None] * c2[None, :] * 0.5))
            return np.clip(rgb, 0, 255)

        # ── orbit ─────────────────────────────────────────────────────────
        elif name == "orbit":
            # Several bright points orbiting centres spaced along the canvas.
            # Points are tiny gaussians — never fills strips.
            rgb  = np.zeros((_N_LEDS, 3), dtype=np.float32)
            sx   = 3.5;  sy = 0.5

            centres = [(_LEDS_STRIP * 0.2, _CY),
                       (_LEDS_STRIP * 0.5, _CY),
                       (_LEDS_STRIP * 0.8, _CY)]
            rx_set  = [_LEDS_STRIP * 0.1, _LEDS_STRIP * 0.12, _LEDS_STRIP * 0.1]
            ry_set  = [(_N_STRIPS-1)*0.42, (_N_STRIPS-1)*0.48, (_N_STRIPS-1)*0.42]

            for ci, ((cx, cy), rx, ry) in enumerate(zip(centres, rx_set, ry_set)):
                n_pts = 2 + ci
                for j in range(n_pts):
                    ph  = j * _TAU / n_pts + ci * 0.7
                    om  = speed * (0.3 + ci * 0.08) * _TAU
                    ox  = cx + rx * math.cos(t * om + ph)
                    oy  = cy + ry * math.sin(t * om + ph + ci * 0.5)
                    g   = np.exp(-((col - ox)**2 / (2*sx**2) +
                                   (row - oy)**2 / (2*sy**2))).astype(np.float32)
                    clr = c1 if (ci + j) % 2 == 0 else c2
                    rgb += g[:, None] * clr[None, :]

            return np.clip(rgb, 0, 255)

        # ── diagonal_cross ────────────────────────────────────────────────
        elif name == "diagonal_cross":
            # Two thin diagonal beams entering from opposite corners, crossing.
            pos  = (beats * speed * 0.22) % 1.0
            w    = max(0.012, size * 0.18)
            d1   = (x - y + 1.0) * 0.5
            d2   = (x + y) * 0.5
            b1   = np.exp(-((d1 - pos)**2) / (2*w*w))
            b2   = np.exp(-((d2 - (1.0 - pos))**2) / (2*w*w))
            rgb  = b1[:, None] * c1[None, :] + b2[:, None] * c2[None, :]
            return rgb.astype(np.float32)

        # ── zigzag ────────────────────────────────────────────────────────
        elif name == "zigzag":
            # Single bright zigzag line threading through all 5 rows.
            freq    = 2.5 / max(size, 0.1)
            y_line  = 0.5 + 0.46 * np.sin((x * freq + beats * speed * 0.55) * _TAU)
            dy      = np.abs(y - y_line)
            sigma   = 0.09
            i_      = np.exp(-(dy**2) / (2*sigma*sigma))
            hue     = (x * 0.4 + beats * speed * 0.025) % 1.0
            hr, hg, hb = _hue_rgb(hue)
            rgb     = np.stack([hr, hg, hb], axis=1) * i_[:, None]
            return rgb.astype(np.float32)

        # ── spotlight ─────────────────────────────────────────────────────
        elif name == "spotlight":
            t_sl  = beats * speed * 0.12
            cx    = 0.5 + 0.40 * math.sin(t_sl * _TAU)
            cy    = 0.5 + 0.40 * math.sin(t_sl * _TAU * 2.0)
            asp   = _N_STRIPS / _LEDS_STRIP * 60
            dx    = x - cx
            dy    = (y - cy) * asp
            dist  = np.sqrt(dx*dx + dy*dy)
            r     = max(0.07, size * 0.5)
            hard  = np.where(dist < r, 1.0, 0.0).astype(np.float32)
            soft  = np.exp(-(dist**2) / (2*(r*2)**2)).astype(np.float32)
            total = hard * 0.85 + soft * 0.4
            return (total[:, None] * c1[None, :]).astype(np.float32)

        # ── corner_burst ──────────────────────────────────────────────────
        elif name == "corner_burst":
            asp     = _N_STRIPS / _LEDS_STRIP * 60
            corners = [(0.0,0.0),(1.0,0.0),(0.0,1.0),(1.0,1.0)]
            radius  = bp * 1.05
            w       = max(0.035, size * 0.28)
            total   = np.zeros(_N_LEDS, dtype=np.float32)
            for cx, cy in corners:
                dx   = x - cx
                dy   = (y - cy) * asp
                dist = np.sqrt(dx*dx + dy*dy)
                ring = np.exp(-((dist - radius)**2) / (2*w*w))
                total += ring
            fade    = math.exp(-bp * 2.5)
            return np.clip(total[:, None] * c1[None, :] * fade, 0, 255).astype(np.float32)

        # ── radial_rings ──────────────────────────────────────────────────
        elif name == "radial_rings":
            freq   = 3.0 + size * 5
            phase  = beats * speed * 28
            wave   = (np.sin(_DIST_C * freq * 0.12 - phase) * 0.5 + 0.5).astype(np.float32)
            # Only light individual ring bands — suppress the dim fill
            sharp  = np.where(wave > 0.6, (wave - 0.6) / 0.4, 0.0).astype(np.float32)
            fade   = np.clip(1.0 - _DIST_C / (_MAX_DIST * 0.9), 0, 1).astype(np.float32)
            rgb    = _lerp_color(c1, c2, wave)
            return (rgb * (sharp * fade)[:, None]).astype(np.float32)

        # ── chevron ───────────────────────────────────────────────────────
        elif name == "chevron":
            tip  = (beats * speed * 0.14) % 1.5 - 0.25
            arm  = 1.4
            dist = np.abs(np.abs(y - 0.5) - np.abs(x - tip) * arm * 0.5)
            w    = max(0.035, size * 0.22)
            i_   = np.exp(-(dist**2) / (2*w*w)) * np.where(x >= tip - 0.04, 1.0, 0.1)
            return i_.astype(np.float32)

        # ── plasma ────────────────────────────────────────────────────────
        elif name == "plasma":
            f  = 7.0 / max(size, 0.05)
            g_ = 5.0 / max(size, 0.05)
            t_ = beats * speed * 0.5
            v  = (0.5 + 0.5*(np.sin(x*f + t_) * np.cos(y*g_ + t_*0.8)
                              + np.sin((x+y)*3.5 + t_*0.6))).astype(np.float32) * 0.5
            # Sharpen so only peaks light up — prevents even fill
            sharp = np.where(v > 0.35, (v - 0.35) / 0.65, 0.0).astype(np.float32)
            return sharp

        # ── fallback ──────────────────────────────────────────────────────
        else:
            # Unknown pattern — sparse twinkle rather than solid fill
            phase = (_LED_R0 * 6.28 + beats * speed * 2.0)
            i_    = np.clip(np.sin(phase) * 0.5 + 0.3, 0, 1).astype(np.float32)
            return np.where(i_ > 0.5, i_, 0.0).astype(np.float32)
