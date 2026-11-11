"""DOWN effects — decaying energy, fading, settling."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap, _palette_array, _tail_mask


class SingleBreath(BaseEffect):
    name     = "SINGLE\nBREATH"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        phase = (t * bpm / 60.0 * speed * 0.25) % 1.0
        amp   = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) ** 3
        arr   = np.arange(n, dtype=float)
        sig   = n * 0.09   # tighter — one visible spot per segment, not a full wash
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(0.5)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class DyingComet(BaseEffect):
    """Stateful comet: decay buffer gives a real lingering tail as it slows and fades."""
    name     = "DYING\nCOMET"
    category = "down"

    _TAIL_DECAY = 0.88   # slower decay — comet trail hangs in the air

    def __init__(self):
        self._buf = None
        self._n   = 0

    def render(self, t, bpm, n, palette, speed):
        if n != self._n:
            self._buf = np.zeros((n, 3), dtype=np.float32)
            self._n   = n

        # Dim all pixels — the tail forms naturally from this
        self._buf *= self._TAIL_DECAY

        # Decelerating head: quadratic ease-in to n, then restarts
        beats     = t * bpm / 60.0 * speed
        bar_phase = (beats / 4.0) % 1.0
        ease      = 1.0 - (1.0 - bar_phase) ** 2
        pos       = ease * n
        amp       = (1.0 - bar_phase) ** 1.2   # head dims as comet slows

        if amp > 0.02:
            head_r, head_g, head_b = palette(bar_phase * 0.7)
            head_sig = max(2.0, n * 0.009)
            arr  = np.arange(n, dtype=np.float32)
            dist = np.minimum((arr - pos) % n, (pos - arr) % n)
            head_m = np.exp(-(dist ** 2) / (2.0 * head_sig ** 2)) * amp

            self._buf[:, 0] = np.maximum(self._buf[:, 0], head_r * head_m * 255)
            self._buf[:, 1] = np.maximum(self._buf[:, 1], head_g * head_m * 255)
            self._buf[:, 2] = np.maximum(self._buf[:, 2], head_b * head_m * 255)

        return np.clip(self._buf, 0, 255).astype(np.uint8)


class SparseDrift(BaseEffect):
    name     = "SPARSE\nDRIFT"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.arange(n, dtype=float)
        n_pts = max(2, int(n * 0.015))
        sig   = n * 0.012
        for i in range(n_pts):
            # Each point drifts slowly at unique speed
            phase = (t * speed * (0.03 + i * 0.007) + i * 0.618) % 1.0
            pos   = phase * n
            amp   = 0.4 + 0.4 * (math.sin(t * 0.4 + i * 1.1) * 0.5 + 0.5)
            m     = _gauss(arr, pos, sig) * amp
            r, g, b = palette((i / n_pts + phase * 0.2) % 1.0)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class EchoPulse(BaseEffect):
    name     = "ECHO\nPULSE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        arr        = np.arange(n, dtype=float)
        center     = n // 2
        n_echoes   = 4
        for e in range(n_echoes):
            delay  = e / n_echoes
            phase  = (beat_phase - delay) % 1.0
            radius = phase * center
            amp    = max(0.0, 1.0 - phase) / (e + 1)
            sig    = n * 0.018
            for sign in (-1, 1):
                pos = center + sign * radius
                m   = _gauss(arr, pos, sig) * amp
                r, g, b = palette(e / n_echoes)
                px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class VoidScan(BaseEffect):
    name     = "VOID\nSCAN"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        phase = (beats / 2.0) % 1.0
        pos   = math.sin(phase * math.pi) * (n - 1)
        arr   = np.arange(n, dtype=float)
        # Background: very dim gradient
        bg_colors = _palette_array(palette, np.linspace(0.0, 1.0, n))
        px[:, 0] = np.clip(bg_colors[:, 0] * 0.15, 0, 255)
        px[:, 1] = np.clip(bg_colors[:, 1] * 0.15, 0, 255)
        px[:, 2] = np.clip(bg_colors[:, 2] * 0.15, 0, 255)
        # Void: dark notch cutting through
        void_sig = n * 0.04
        void_m   = _gauss(arr, pos, void_sig)
        px[:, 0] = np.clip(px[:, 0] * (1.0 - void_m * 0.9), 0, 255)
        px[:, 1] = np.clip(px[:, 1] * (1.0 - void_m * 0.9), 0, 255)
        px[:, 2] = np.clip(px[:, 2] * (1.0 - void_m * 0.9), 0, 255)
        return px


class LongBreath(BaseEffect):
    name     = "LONG\nBREATH"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        phase = (t * speed * 0.12) % 1.0
        amp   = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) ** 2 * 0.8
        arr   = np.arange(n, dtype=float)
        sig   = n * 0.11   # tighter — slow deep breath, not a full-strip flood
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(0.45)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class Afterglow(BaseEffect):
    name     = "AFTER\nGLOW"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        # 3 warm spots — one near each end and center — slowly fading and drifting
        fade = max(0.05, math.exp(-t * 0.04 * speed) * 0.75 + 0.1)
        sig  = n * 0.055
        spots = [0.2, 0.5, 0.8]
        for i, centre in enumerate(spots):
            drift = math.sin(t * 0.08 * speed + i * 2.1) * 0.04
            pos   = (centre + drift) * n
            amp   = fade * (0.85 + 0.15 * math.sin(t * 0.13 * speed + i))
            m     = _gauss(arr, pos, sig) * amp
            r, g, b = palette(centre * 0.5)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class MemoryFade(BaseEffect):
    name     = "MEMORY\nFADE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        # pixels hold their color but slowly dim
        amp       = (1.0 - bar_phase) ** 1.5 * 0.85
        colors    = _palette_array(palette, arr)
        px[:, 0]  = np.clip(colors[:, 0] * amp, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * amp, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * amp, 0, 255)
        return px


class TiredPulse(BaseEffect):
    name     = "TIRED\nPULSE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        phase = beats % 1.0
        arr   = np.arange(n, dtype=float)
        sig   = n * 0.15
        amp   = max(0.0, 1.0 - phase * 3) * 0.6
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(0.3)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class SettleDust(BaseEffect):
    name     = "SETTLE\nDUST"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        sig  = max(1.2, n * 0.007)
        rng  = np.random.RandomState(7)
        n_segs = max(1, n // 300)
        n_dust = max(3, n // 60)
        for i in range(n_dust):
            # each speck falls toward end, dims as it goes
            start_pos = rng.uniform(0, n)
            fall_rate = rng.uniform(0.3, 1.0)
            pos       = (start_pos + t * fall_rate * speed * 8.0 * n_segs) % n
            amp       = max(0.0, 1.0 - (pos / n) * 0.8) * 0.6
            m         = _gauss(arr, pos, sig) * amp
            r, g, b   = palette(i / n_dust)
            px[:, 0]  = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1]  = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2]  = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class WaveDamp(BaseEffect):
    name     = "WAVE\nDAMP"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        damp      = (1.0 - bar_phase) ** 2
        wave      = np.sin(arr * 4 * math.pi - t * speed * 1.5) * 0.5 + 0.5
        mask      = wave * damp * 0.8
        colors    = _palette_array(palette, arr)
        px[:, 0]  = np.clip(colors[:, 0] * mask, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * mask, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * mask, 0, 255)
        return px


class LastLight(BaseEffect):
    name     = "LAST\nLIGHT"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        pos    = (beats * 0.5 * n_segs) % n
        amp  = max(0.0, 1.0 - (pos / n) * 0.7) * 0.9
        arr  = np.arange(n, dtype=float)
        sig  = max(2.0, n * 0.018)
        m    = _gauss(arr, pos, sig) * amp
        r, g, b = palette(pos / n)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class NightFall(BaseEffect):
    name     = "NIGHT\nFALL"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        # darkness creeps in from both edges toward center
        shadow    = np.minimum(arr, 1.0 - arr) * 2.0  # 0 at edges, 1 at center
        visible   = np.clip(shadow - bar_phase * 1.2, 0.0, 1.0)
        colors    = _palette_array(palette, arr)
        px[:, 0]  = np.clip(colors[:, 0] * visible, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * visible, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * visible, 0, 255)
        return px


class SleepBreath(BaseEffect):
    name     = "SLEEP\nBREATH"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        phase = (t * speed * 0.07) % 1.0
        amp   = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) * 0.5
        arr   = np.linspace(0.0, 1.0, n)
        colors = _palette_array(palette, arr * 0.3 + 0.35)
        px[:, 0] = np.clip(colors[:, 0] * amp, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * amp, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * amp, 0, 255)
        return px


class RumbleEcho(BaseEffect):
    name     = "RUMBLE\nECHO"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.linspace(0.0, 1.0, n)
        # low-level trembling shimmer
        rumble = (np.sin(arr * 6.3 + t * speed * 3.0) * 0.5 + 0.5) * \
                 (np.cos(arr * 2.7 - t * speed * 1.8) * 0.5 + 0.5)
        amp    = 0.3
        colors = _palette_array(palette, arr * 0.5)
        px[:, 0] = np.clip(colors[:, 0] * rumble * amp, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * rumble * amp, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * rumble * amp, 0, 255)
        return px


class OceanWave(BaseEffect):
    """Two sine waves traveling in opposite directions — visible interference crests."""
    name     = "OCEAN\nWAVE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        arr   = np.linspace(0.0, 1.0, n, dtype=np.float32)
        wave1 = np.sin(arr * 6.0 * math.pi - t * speed * 1.7).astype(np.float32) * 0.5 + 0.5
        wave2 = np.sin(arr * 3.5 * math.pi + t * speed * 1.1).astype(np.float32) * 0.35 + 0.35
        combined = np.clip((wave1 + wave2) / 1.7, 0.0, 1.0)
        colors = _palette_array(palette, arr * 0.7 + 0.1)
        px = np.zeros((n, 3), dtype=np.uint8)
        px[:, 0] = np.clip(colors[:, 0] * combined, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * combined, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * combined, 0, 255)
        return px


class DeepSine(BaseEffect):
    """One slow large sine band traveling through the strip — a single visible wave."""
    name     = "DEEP\nSINE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        arr  = np.linspace(0.0, 1.0, n, dtype=np.float32)
        wave = np.sin(arr * 2.5 * math.pi - t * speed * 1.3).astype(np.float32) * 0.5 + 0.5
        # Keep a dim floor so dark parts still show something
        wave = wave * 0.78 + 0.08
        colors = _palette_array(palette, arr * 0.85)
        px = np.zeros((n, 3), dtype=np.uint8)
        px[:, 0] = np.clip(colors[:, 0] * wave, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * wave, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * wave, 0, 255)
        return px


class EmberDrift(BaseEffect):
    """20 warm embers drifting at near-zero speed with independent glow cycles.
    Unlike comets (one direction) — embers float in place with gentle heat shimmer.
    Use warm palette (Fire / Amber) for best effect."""
    name     = "EMBER\nDRIFT"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        px       = np.zeros((n, 3), dtype=np.uint8)
        arr      = np.arange(n, dtype=np.float32)
        rng      = np.random.RandomState(31)
        base_pos = rng.uniform(0, n, 20).astype(np.float32)
        drifts   = rng.uniform(-0.05, 0.05, 20).astype(np.float32) * float(speed)
        phases   = rng.uniform(0, 2 * math.pi, 20).astype(np.float32)
        hues     = rng.uniform(0.05, 0.40, 20).astype(np.float32)   # warm range
        sig      = max(1.5, n * 0.006)
        ts       = float(t)
        for i in range(20):
            pos = (base_pos[i] + drifts[i] * ts * 30) % n
            amp = (math.sin(ts * 0.6 + float(phases[i])) * 0.25 + 0.75) * 0.65
            m   = _gauss(arr, pos, sig) * amp
            r, g, b = palette(float(hues[i]))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class AuroraWave(BaseEffect):
    """Two slow sine bands shift the full palette across the strip continuously.
    Smooth, atmospheric, full-strip colour — very different from point-source effects."""
    name     = "AURORA\nWAVE"
    category = "down"

    def render(self, t, bpm, n, palette, speed):
        ts     = np.float32(float(t) * float(speed) * 0.12)
        x      = np.linspace(0.0, 2 * math.pi, n, dtype=np.float32)
        wave1  = np.sin(x * 1.5 + ts * 1.1) * np.float32(0.5) + np.float32(0.5)
        wave2  = np.sin(x * 0.7 - ts * 0.8) * np.float32(0.5) + np.float32(0.5)
        hue    = wave1 * np.float32(0.6) + wave2 * np.float32(0.4)
        colors = _palette_array(palette, hue)
        env    = (np.sin(x * 0.5) ** 0.5).astype(np.float32)
        return np.clip(colors * env[:, np.newaxis] * 0.85, 0, 255).astype(np.uint8)


# Removed: SparseDrift (near-dup of EmberDrift/Afterglow), DeepSine (near-dup of AuroraWave)
EFFECTS = [DyingComet, Afterglow, WaveDamp, NightFall, EmberDrift, AuroraWave]
