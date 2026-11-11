"""UP effects — building energy, accelerating, converging."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap, _palette_array, _tail_mask


class AcceleratingScan(BaseEffect):
    name     = "ACCEL\nSCAN"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        bar_phase  = (beats / (4.0 * n_segs)) % 1.0
        pos        = bar_phase ** 2 * n
        arr        = np.arange(n, dtype=float)
        sig        = n * 0.01
        m          = _gauss(arr, pos, sig)
        tail_m     = _tail_mask(arr, pos, n * 0.08, power=1.5) * 0.6
        r, g, b    = palette(bar_phase)
        total      = np.clip(m + tail_m, 0, 1)
        px[:, 0]   = np.clip(r * total, 0, 255)
        px[:, 1]   = np.clip(g * total, 0, 255)
        px[:, 2]   = np.clip(b * total, 0, 255)
        return px


class StackingPulses(BaseEffect):
    name     = "STACKING\nPULSES"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        bar_count  = int(beats / 4.0)
        bar_phase  = (beats / 4.0) % 1.0
        arr        = np.arange(n, dtype=float)
        n_beats_in = int(beats % 4) + 1  # accumulates through bar
        for i in range(min(n_beats_in, 4)):
            pos  = (i + 1) * n // 5
            sig  = n * 0.035 * (1.0 + bar_phase * 0.5)
            amp  = 0.5 + 0.5 * bar_phase
            m    = _gauss(arr, float(pos), sig) * amp
            r, g, b = palette(i / 4.0)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class PressureBeams(BaseEffect):
    name     = "PRESSURE\nBEAMS"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        n_segs    = max(1, n // 300)
        bar_phase = (t * bpm / 60.0 * speed / (4.0 * n_segs)) % 1.0
        arr       = np.arange(n, dtype=float)
        # Two beams creeping from ends toward center
        left_pos  = bar_phase * (n // 2)
        right_pos = n - 1 - bar_phase * (n // 2)
        sig       = n * 0.025
        m_l       = _gauss(arr, left_pos,  sig)
        m_r       = _gauss(arr, right_pos, sig)
        # Fill behind beams with dim glow
        glow_l    = np.where(arr <= left_pos,  bar_phase * 0.3, 0.0)
        glow_r    = np.where(arr >= right_pos, bar_phase * 0.3, 0.0)
        r, g, b   = palette(0.1)
        r2, g2, b2 = palette(0.8)
        px[:, 0] = np.clip(r * m_l + r2 * m_r + r * glow_l + r2 * glow_r, 0, 255)
        px[:, 1] = np.clip(g * m_l + g2 * m_r + g * glow_l + g2 * glow_r, 0, 255)
        px[:, 2] = np.clip(b * m_l + b2 * m_r + b * glow_l + b2 * glow_r, 0, 255)
        return px


class RisingComets(BaseEffect):
    name     = "RISING\nCOMETS"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        arr    = np.arange(n, dtype=float)
        n_segs = max(1, n // 300)
        for i in range(2):
            offset = i / 2
            phase  = (beats * 0.6 * n_segs + offset) % 1.0
            pos    = phase * n
            sig    = n * 0.010
            m      = _gauss(arr, pos, sig)
            tail_m = _tail_mask(arr, pos, n * 0.07) * 0.7
            r, g, b  = palette((i / 2 + beats * 0.08) % 1.0)
            total    = np.clip(m + tail_m, 0, 1)
            px[:, 0] = np.clip(px[:, 0] + r * total, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * total, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * total, 0, 255)
        return px


class ConvergingLasers(BaseEffect):
    name     = "CONVERGE\nLASERS"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        n_segs    = max(1, n // 300)
        bar_phase = (t * bpm / 60.0 * speed / (2.0 * n_segs)) % 1.0
        arr       = np.arange(n, dtype=float)
        center    = n // 2
        sig       = max(1.0, n * 0.007)
        n_pairs   = 2
        for i in range(n_pairs):
            frac   = (i + 1) / (n_pairs + 1)
            # Each pair converges at a different beat sub-offset
            phase  = (bar_phase + i * 0.2) % 1.0
            dist   = (1.0 - phase) * frac * center
            p_l    = center - dist
            p_r    = center + dist
            amp    = phase ** 0.5   # brightens as they approach
            m_l    = _gauss(arr, p_l, sig) * amp
            m_r    = _gauss(arr, p_r, sig) * amp
            r, g, b = palette(frac)
            px[:, 0] = np.clip(px[:, 0] + r * (m_l + m_r), 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * (m_l + m_r), 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * (m_l + m_r), 0, 255)
        return px


class PumpRise(BaseEffect):
    name     = "PUMP\nRISE"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        bar_phase  = (t * bpm / 60.0 * speed / 4.0) % 1.0
        pump       = max(0.0, 1.0 - beat_phase * 2.5) * 0.7 + bar_phase * 0.3
        colors     = _palette_array(palette, np.linspace(0.0, 1.0, n))
        px[:, 0]   = np.clip(colors[:, 0] * pump, 0, 255)
        px[:, 1]   = np.clip(colors[:, 1] * pump, 0, 255)
        px[:, 2]   = np.clip(colors[:, 2] * pump, 0, 255)
        return px


class LaunchSequence(BaseEffect):
    name     = "LAUNCH\nSEQ"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        n_segs    = 8
        seg_w     = n // n_segs
        n_lit     = int(bar_phase * n_segs)  # how many segs lit so far
        for s in range(n_lit):
            r, g, b = palette(s / n_segs)
            start = s * seg_w; end = min(n, start + seg_w)
            px[start:end, 0] = int(r * 0.85)
            px[start:end, 1] = int(g * 0.85)
            px[start:end, 2] = int(b * 0.85)
        # currently lighting segment
        if n_lit < n_segs:
            frac  = (bar_phase * n_segs) % 1.0
            r, g, b = palette(n_lit / n_segs)
            start = n_lit * seg_w; end = min(n, start + seg_w)
            px[start:end, 0] = int(r * frac)
            px[start:end, 1] = int(g * frac)
            px[start:end, 2] = int(b * frac)
        return px


class ChargeUp(BaseEffect):
    name     = "CHARGE\nUP"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        n_segs    = max(1, n // 300)
        bar_phase = (t * bpm / 60.0 * speed / (4.0 * n_segs)) % 1.0
        arr       = np.arange(n, dtype=float)
        center    = n // 2
        fill      = bar_phase * center
        # fill from both ends inward
        left_end  = int(fill)
        right_start = n - int(fill)
        colors    = _palette_array(palette, np.linspace(0.0, 1.0, n))
        amp       = 0.4 + 0.6 * bar_phase
        if left_end > 0:
            px[:left_end, 0]      = np.clip(colors[:left_end, 0] * amp, 0, 255)
            px[:left_end, 1]      = np.clip(colors[:left_end, 1] * amp, 0, 255)
            px[:left_end, 2]      = np.clip(colors[:left_end, 2] * amp, 0, 255)
        if right_start < n:
            px[right_start:, 0]   = np.clip(colors[right_start:, 0] * amp, 0, 255)
            px[right_start:, 1]   = np.clip(colors[right_start:, 1] * amp, 0, 255)
            px[right_start:, 2]   = np.clip(colors[right_start:, 2] * amp, 0, 255)
        return px


class TurbineBeam(BaseEffect):
    name     = "TURBINE\nBEAM"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        n_segs    = max(1, n // 300)
        # Beam spins faster and faster through the bar
        spin_rate = 1.0 + bar_phase * 4.0
        pos       = (t * bpm / 60.0 * speed * spin_rate * n_segs) % n
        arr       = np.arange(n, dtype=float)
        sig       = max(1.5, n * 0.01)
        m         = _gauss_wrap(arr, pos, sig, n)
        r, g, b   = palette(bar_phase)
        px[:, 0]  = np.clip(r * m, 0, 255)
        px[:, 1]  = np.clip(g * m, 0, 255)
        px[:, 2]  = np.clip(b * m, 0, 255)
        return px


class BurstRepeat(BaseEffect):
    name     = "BURST\nREPEAT"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        arr       = np.arange(n, dtype=float)
        sig       = max(2.0, n * 0.02)
        # intensity of each burst grows over the bar
        burst_amp = 0.3 + 0.7 * bar_phase
        amp       = max(0.0, 1.0 - beat_phase * 3) * burst_amp
        if amp > 0.01:
            beat_count = int(t * bpm / 60.0 * speed)
            r, g, b    = palette(beat_count * 0.13 % 1.0)
            center     = n // 2
            m          = _gauss(arr, float(center), sig * (1.0 + bar_phase)) * amp
            px[:, 0]   = np.clip(r * m, 0, 255)
            px[:, 1]   = np.clip(g * m, 0, 255)
            px[:, 2]   = np.clip(b * m, 0, 255)
        return px


class FrenzyChase(BaseEffect):
    name     = "FRENZY\nCHASE"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.arange(n, dtype=float)
        sig       = max(1.0, n * 0.008)
        rate      = 2.0 + bar_phase * 6.0  # gets faster
        beats     = t * bpm / 60.0 * speed
        n_segs    = max(1, n // 300)
        for i in range(3):
            pos     = (beats * rate * n_segs + i * n / 3.0) % n
            m       = _gauss_wrap(arr, pos, sig, n)
            r, g, b = palette(i / 3.0)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class EscalatorSteps(BaseEffect):
    name     = "ESCALA\nTOR"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0 * speed)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        n_steps    = 8
        step_w     = n // n_steps
        # each new beat lights the next step brighter
        for s in range(n_steps):
            if s <= beat_count % n_steps:
                bright  = 0.3 + 0.7 * (s / n_steps)
                if s == beat_count % n_steps:
                    bright *= max(0.0, 1.0 - beat_phase * 0.5) + beat_phase * 0.5
                r, g, b = palette(s / n_steps)
                start = s * step_w; end = min(n, start + step_w)
                px[start:end, 0] = int(r * bright)
                px[start:end, 1] = int(g * bright)
                px[start:end, 2] = int(b * bright)
        return px


class RampLaser(BaseEffect):
    name     = "RAMP\nLASER"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        beats     = t * bpm / 60.0 * speed
        n_segs    = max(1, n // 300)
        pos       = (beats * (1.0 + bar_phase * 3.0) * n_segs) % n
        arr       = np.arange(n, dtype=float)
        sig       = max(1.0, n * 0.007)
        m         = _gauss_wrap(arr, pos, sig, n)
        r, g, b   = palette(bar_phase)
        px[:, 0]  = np.clip(r * m, 0, 255)
        px[:, 1]  = np.clip(g * m, 0, 255)
        px[:, 2]  = np.clip(b * m, 0, 255)
        return px


class CrescendoFlash(BaseEffect):
    name     = "CRESC\nFLASH"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        bar_phase  = (t * bpm / 60.0 * speed / 4.0) % 1.0
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        beat_count = int(t * bpm / 60.0 * speed)
        # flash amplitude increases each beat within bar
        flash_amp  = max(0.0, 1.0 - beat_phase * 4) * (0.3 + 0.7 * bar_phase)
        if flash_amp > 0.01:
            r, g, b = palette(beat_count * 0.11 % 1.0)
            px[:, 0] = int(r * flash_amp)
            px[:, 1] = int(g * flash_amp)
            px[:, 2] = int(b * flash_amp)
        return px


class DrumBuild(BaseEffect):
    name     = "DRUM\nBUILD"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        bar_phase  = (beats / 4.0) % 1.0
        beat_phase = beats % 1.0
        arr        = np.arange(n, dtype=float)
        # Each beat hits harder, positions spread out more as bar builds
        n_hits = int(beats % 4) + 1
        sig    = max(2.0, n * 0.018)
        for h in range(n_hits):
            pos     = (h + 1) * n / (n_hits + 1)
            amp     = max(0.0, 1.0 - beat_phase * 3) * (0.4 + 0.6 * bar_phase)
            m       = _gauss(arr, pos, sig) * amp
            r, g, b = palette(h / max(1, n_hits))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class PreChorusBlackout(BaseEffect):
    """Near-silence for tension before a chorus drop. Use when next_phrase_type == 'chorus'."""
    name     = "PRE\nBLACK"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        return np.zeros((n, 3), dtype=np.uint8)


class RandomBlockStrobe(BaseEffect):
    """Random-position colored blocks that strobe on/off, firing faster as speed rises.
    Blocks each pick a different palette hue so the stage looks like multi-color hits."""
    name     = "RAND\nBLOCK"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        # Strobe rate: 2–6 Hz, scales with speed
        strobe_hz  = 2.0 + speed * 2.5
        strobe_ph  = (t * strobe_hz) % 1.0
        if strobe_ph > 0.45:          # off half of the cycle
            return px
        # Number of simultaneous blocks increases with speed
        n_blocks   = max(2, int(2 + speed * 2))
        block_w    = max(6, n // 10)  # each block ~10% strip width
        rng        = np.random.default_rng(int(beats * 4) & 0xFFFFFF)
        positions  = rng.integers(0, n - block_w, size=n_blocks)
        hues       = rng.uniform(0, 1, size=n_blocks)
        for i, (pos, hue) in enumerate(zip(positions, hues)):
            r, g, b = palette(hue)
            end = int(pos) + block_w
            px[int(pos):end, 0] = np.clip(r, 0, 255)
            px[int(pos):end, 1] = np.clip(g, 0, 255)
            px[int(pos):end, 2] = np.clip(b, 0, 255)
        return px


class FloodFill(BaseEffect):
    """Strip fills from left with palette gradient over 4 beats, then resets.
    Bright leading edge marks the frontier — a rising tide of colour."""
    name     = "FLOOD\nFILL"
    category = "up"

    def render(self, t, bpm, n, palette, speed):
        beats     = t * bpm / 60.0 * speed
        bar_phase = (beats / 4.0) % 1.0
        n_lit     = max(1, int(bar_phase * n))
        arr_lit   = np.linspace(0.0, 1.0, n_lit, dtype=np.float32)
        canvas    = np.zeros((n, 3), dtype=np.float32)
        canvas[:n_lit] = _palette_array(palette, arr_lit)
        # Bright edge pulse at the frontier
        arr_all    = np.arange(n, dtype=np.float32)
        edge       = _gauss(arr_all, float(n_lit - 1), max(2.0, n * 0.008))
        r, g, b    = palette(bar_phase)
        canvas[:, 0] += r * edge * 1.5
        canvas[:, 1] += g * edge * 1.5
        canvas[:, 2] += b * edge * 1.5
        return np.clip(canvas, 0, 255).astype(np.uint8)


class FrequencyBands(BaseEffect):
    """5 strip sections oscillate at incommensurate frequencies — like 5 EQ bars.
    Each band has its own colour and phase. Hard-edged blocks, no blur."""
    name     = "FREQ\nBANDS"
    category = "up"

    _FREQS = (0.31, 0.47, 0.63, 0.83, 1.07)   # deliberately non-harmonic

    def render(self, t, bpm, n, palette, speed):
        ts     = float(t) * float(speed)
        canvas = np.zeros((n, 3), dtype=np.float32)
        seg_w  = n // 5
        for i, freq in enumerate(self._FREQS):
            amp   = (math.sin(ts * freq * 2 * math.pi) * 0.5 + 0.5) ** 1.8
            r, g, b = palette(i / 4.0)
            s, e    = i * seg_w, min(n, (i + 1) * seg_w)
            canvas[s:e, 0] = r * amp
            canvas[s:e, 1] = g * amp
            canvas[s:e, 2] = b * amp
        return np.clip(canvas, 0, 255).astype(np.uint8)


# Removed: ConvergingLasers (near-dup of PressureBeams), CrescendoFlash (near-dup of strobe)
EFFECTS = [PressureBeams, TurbineBeam, FrenzyChase, EscalatorSteps,
           RandomBlockStrobe, FloodFill, FrequencyBands]
