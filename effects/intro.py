"""INTRO effects — sparse, gentle, building."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap, _palette_array, _tail_mask


class SinglePixelCrawl(BaseEffect):
    name     = "SINGLE\nCRAWL"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        n_segs = max(1, n // 300)
        pos    = (t * bpm / 60.0 * speed * 6.0 * n_segs) % n
        pos2   = (n - 1) - pos   # mirror pixel
        arr  = np.arange(n, dtype=float)
        sig  = n * 0.012
        m1   = _gauss(arr, pos,  sig)
        m2   = _gauss(arr, pos2, sig) * 0.4
        r, g, b = palette(pos / n)
        px[:, 0] = np.clip((r * m1 + r * m2), 0, 255)
        px[:, 1] = np.clip((g * m1 + g * m2), 0, 255)
        px[:, 2] = np.clip((b * m1 + b * m2), 0, 255)
        return px


class BreathSpot(BaseEffect):
    name     = "BREATH\nSPOT"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        phase  = (t * bpm / 60.0 * speed * 0.5) % 1.0
        amp    = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) ** 2
        arr    = np.arange(n, dtype=float)
        center = n * (0.3 + 0.4 * (math.sin(t * 0.11) * 0.5 + 0.5))
        sig    = n * 0.08 * (0.5 + 0.5 * amp)
        m      = _gauss(arr, center, sig) * amp
        r, g, b = palette(phase)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class SparkleField(BaseEffect):
    name     = "SPARKLE\nFIELD"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0)
        beat_phase = (t * bpm / 60.0) % 1.0
        rng        = np.random.RandomState(beat_count * 7 + 13)
        n_sparks   = max(4, int(n * 0.03 * speed))
        positions  = rng.randint(0, n, n_sparks)
        lifetimes  = rng.uniform(0.1, 1.0, n_sparks)
        color_pos  = rng.uniform(0.0, 1.0, n_sparks)
        arr        = np.arange(n, dtype=float)
        sig        = max(1.5, n * 0.006)
        for i, (pos, lt, cp) in enumerate(zip(positions, lifetimes, color_pos)):
            fade = max(0.0, 1.0 - beat_phase / lt)
            if fade < 0.01:
                continue
            m        = _gauss(arr, float(pos), sig) * fade
            r, g, b  = palette(cp)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class GradientWipe(BaseEffect):
    name     = "GRADIENT\nWIPE"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        bar_ph = (t * bpm / 60.0 / 4.0 * speed) % 1.0
        # Gradient that sweeps: only a window is lit
        window = 0.35
        arr    = np.linspace(0.0, 1.0, n)
        # center of window moves across strip each bar
        center = bar_ph
        dist   = np.abs(arr - center)
        mask   = np.clip(1.0 - dist / (window * 0.5), 0, 1) ** 2
        colors = _palette_array(palette, (arr + bar_ph * 0.5) % 1.0)
        px[:, 0] = np.clip(colors[:, 0] * mask, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * mask, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * mask, 0, 255)
        return px


class SoftComet(BaseEffect):
    name     = "SOFT\nCOMET"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        # bounce back-and-forth across 2n range
        cycle  = (beats * 4.0 * n_segs) % (2 * n)
        pos   = cycle if cycle < n else (2 * n - cycle)
        arr   = np.arange(n, dtype=float)
        # head
        head_sig = n * 0.025
        head_m   = _gauss(arr, pos, head_sig)
        # tail (vectorized)
        direction = 1.0 if cycle < n else -1.0
        tail_len  = n * 0.12
        tail_m    = _tail_mask(arr * direction, pos * direction, tail_len) * 0.6
        m        = np.clip(head_m + tail_m, 0, 1)
        r, g, b  = palette(pos / n)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class DualBreath(BaseEffect):
    name     = "DUAL\nBREATH"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.arange(n, dtype=float)
        beats = t * bpm / 60.0 * speed
        for i, frac in enumerate([0.25, 0.75]):
            amp = (math.sin(beats * math.pi * 0.5 + i * math.pi) * 0.5 + 0.5) ** 2
            sig = n * 0.07
            m   = _gauss(arr, n * frac, sig) * amp
            r, g, b = palette(frac)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class NightSky(BaseEffect):
    name     = "NIGHT\nSKY"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        sig  = max(1.2, n * 0.006)
        rng  = np.random.RandomState(17)
        positions = rng.randint(0, n, max(3, n // 80))
        for i, pos in enumerate(positions):
            twinkle = abs(math.sin(t * (0.3 + i * 0.07) * speed + i))
            m       = _gauss(arr, float(pos), sig) * twinkle * 0.8
            r, g, b = palette(i / len(positions))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class PulseRipple(BaseEffect):
    name     = "PULSE\nRIPPLE"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        # One pulse every 4 beats — soothing slow ripple at speed=0.5
        beats      = t * bpm / 60.0 * speed
        arr        = np.arange(n, dtype=float)
        center     = n // 2
        for ring in range(3):
            delay  = ring * (4.0 / 3.0)          # 3 rings spread over 4 beats
            ph     = ((beats - delay) / 4.0) % 1.0
            radius = ph * center
            amp    = max(0.0, 1.0 - ph) * (1.0 - ring * 0.3)
            sig    = max(2.0, n * 0.022)
            for sign in (-1, 1):
                pos = center + sign * radius
                m   = _gauss(arr, pos, sig) * amp
                r, g, b = palette(ring / 3.0)
                px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class TwilightWave(BaseEffect):
    name     = "TWILIGHT\nWAVE"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px  = np.zeros((n, 3), dtype=np.uint8)
        arr = np.linspace(0.0, 1.0, n)
        wave = np.sin(arr * 2 * math.pi - t * speed * 0.8) * 0.5 + 0.5
        wave = wave ** 2 * 0.7
        colors = _palette_array(palette, (arr + t * 0.05 * speed) % 1.0)
        px[:, 0] = np.clip(colors[:, 0] * wave, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * wave, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * wave, 0, 255)
        return px


class HeartbeatDot(BaseEffect):
    name     = "HEART\nBEAT"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        phase = beats % 1.0
        arr   = np.arange(n, dtype=float)
        sig   = n * 0.025
        center = n // 2
        # double-thump pattern
        a1 = max(0.0, 1.0 - phase * 8) if phase < 0.125 else 0.0
        a2 = max(0.0, 1.0 - (phase - 0.18) * 8) if 0.18 <= phase < 0.305 else 0.0
        amp = max(a1, a2)
        if amp > 0.01:
            m       = _gauss(arr, float(center), sig) * amp
            r, g, b = palette(0.4)
            px[:, 0] = np.clip(r * m, 0, 255)
            px[:, 1] = np.clip(g * m, 0, 255)
            px[:, 2] = np.clip(b * m, 0, 255)
        return px


class FogDrift(BaseEffect):
    name     = "FOG\nDRIFT"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px  = np.zeros((n, 3), dtype=np.uint8)
        arr = np.linspace(0.0, 1.0, n)
        fog = (np.sin(arr * 3.1 + t * 0.2 * speed) * 0.5 + 0.5) * \
              (np.cos(arr * 1.7 - t * 0.15 * speed) * 0.5 + 0.5)
        fog = fog * 0.45
        colors = _palette_array(palette, arr)
        px[:, 0] = np.clip(colors[:, 0] * fog, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * fog, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * fog, 0, 255)
        return px


class ChimeSpots(BaseEffect):
    name     = "CHIME\nSPOTS"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0 * speed * 0.5)
        beat_phase = (t * bpm / 60.0 * speed * 0.5) % 1.0
        rng        = np.random.RandomState(beat_count + 3)
        arr        = np.arange(n, dtype=float)
        sig        = max(2.0, n * 0.025)
        n_chimes   = 4
        for i in range(n_chimes):
            pos    = rng.randint(n // 6, 5 * n // 6)
            delay  = i * 0.15
            ph     = max(0.0, beat_phase - delay)
            amp    = math.exp(-ph * 4.0)
            m      = _gauss(arr, float(pos), sig) * amp
            r, g, b = palette(rng.uniform(0.1, 0.9))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class CrawlPair(BaseEffect):
    name     = "CRAWL\nPAIR"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        sig    = max(1.5, n * 0.012)
        n_segs = max(1, n // 300)
        p1     = (t * bpm / 60.0 * speed * 4.0 * n_segs) % n
        p2     = (t * bpm / 60.0 * speed * 2.5 * n_segs + n * 0.5) % n
        for pos, cp in ((p1, 0.1), (p2, 0.6)):
            m       = _gauss_wrap(arr, pos, sig, n)
            r, g, b = palette(cp)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class SlowFlash(BaseEffect):
    name     = "SLOW\nFLASH"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        bar_phase  = (t * bpm / 60.0 * speed / 4.0) % 1.0
        amp        = max(0.0, 1.0 - bar_phase * 6) if bar_phase < 0.16 else 0.0
        if amp > 0.01:
            colors = np.array([palette(i / n) for i in range(n)], dtype=float)
            px[:, 0] = np.clip(colors[:, 0] * amp, 0, 255)
            px[:, 1] = np.clip(colors[:, 1] * amp, 0, 255)
            px[:, 2] = np.clip(colors[:, 2] * amp, 0, 255)
        return px


class GlowCenter(BaseEffect):
    name     = "GLOW\nCENTER"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        phase = (beats * 0.25) % 1.0
        arr   = np.arange(n, dtype=float)
        amp   = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) ** 2
        sig   = n * (0.06 + 0.12 * amp)
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(phase)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class BreathingPalette(BaseEffect):
    """Full-strip palette gradient that slowly drifts and breathes on every beat.
    Every LED shows a different palette colour — the whole strip is one canvas.
    Totally distinct from point-source effects."""
    name     = "BREATH\nPALETTE"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        beats  = t * bpm / 60.0 * speed
        shift  = (beats * 0.04) % 1.0
        arr    = (np.linspace(0.0, 1.0, n, dtype=np.float32) + shift) % 1.0
        colors = _palette_array(palette, arr)              # (n, 3) float32 0-255
        breath = (math.sin(beats * math.pi) * 0.4 + 0.6) ** 2
        return np.clip(colors * breath, 0, 255).astype(np.uint8)


class PixelDust(BaseEffect):
    """30 dust motes floating at near-zero velocity, each with independent glow.
    Creates a field of floating embers — nothing moves fast, everything shimmers."""
    name     = "PIXEL\nDUST"
    category = "intro"

    def render(self, t, bpm, n, palette, speed):
        px       = np.zeros((n, 3), dtype=np.uint8)
        arr      = np.arange(n, dtype=np.float32)
        rng      = np.random.RandomState(17)
        n_motes  = 30
        base_pos = rng.uniform(0, n, n_motes).astype(np.float32)
        drifts   = rng.uniform(-0.06, 0.06, n_motes).astype(np.float32) * float(speed)
        freqs    = rng.uniform(0.3, 1.1, n_motes).astype(np.float32)
        hues     = rng.uniform(0.0, 1.0, n_motes).astype(np.float32)
        sig      = max(1.0, n * 0.005)
        ts       = float(t)
        for i in range(n_motes):
            pos = (base_pos[i] + drifts[i] * ts * 30) % n
            amp = (math.sin(ts * float(freqs[i]) * 2 * math.pi) * 0.3 + 0.7) * 0.75
            m   = _gauss(arr, pos, sig) * amp
            r, g, b = palette(float(hues[i]))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


# Removed from EFFECTS: TwilightWave (near-dup of FogDrift), GlowCenter (static single blob)
EFFECTS = [SparkleField, NightSky, FogDrift, PulseRipple, BreathingPalette, PixelDust]
