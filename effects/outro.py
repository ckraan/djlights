"""OUTRO effects — dissolving, ending, extinguishing."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap


class FadedownSweep(BaseEffect):
    name     = "FADEDOWN\nSWEEP"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        # Brightness envelope: fades from left to right over the bar
        mask      = np.clip(1.0 - (arr - (1.0 - bar_phase)) * 8, 0, 1) * (1.0 - bar_phase * 0.7)
        colors    = np.array([palette(arr[i]) for i in range(n)], dtype=float)
        px[:, 0]  = np.clip(colors[:, 0] * mask, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * mask, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * mask, 0, 255)
        return px


class PixelExtinction(BaseEffect):
    name     = "PIXEL\nEXTINCT"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        bar_phase  = (t * bpm / 60.0 * speed / 4.0) % 1.0
        # Draw N pixels; progressively fewer remain as bar progresses
        n_visible  = max(1, int(n * (1.0 - bar_phase)))
        rng        = np.random.RandomState(42)
        positions  = rng.choice(n, n_visible, replace=False)
        arr        = np.arange(n, dtype=float)
        sig        = max(1.5, n * 0.008)
        for pos in positions:
            amp     = 0.5 + 0.5 * math.sin(t * 3.0 + pos * 0.1)
            m       = _gauss(arr, float(pos), sig) * amp
            r, g, b = palette(pos / n)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class TrailingOff(BaseEffect):
    name     = "TRAILING\nOFF"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        arr    = np.arange(n, dtype=float)
        # Comets get shorter and dimmer over bars
        bar_phase = (beats / 4.0) % 1.0
        n_comets  = max(1, int(4 * (1.0 - bar_phase)))
        for i in range(n_comets):
            phase = (beats * 0.4 * n_segs + i / max(1, n_comets)) % 1.0
            pos   = phase * n
            amp   = (1.0 - bar_phase) * (1.0 - phase * 0.5)
            sig   = n * 0.010 * (1.0 - bar_phase * 0.7)
            m     = _gauss(arr, pos, sig) * amp
            tail_len = n * 0.05 * (1.0 - bar_phase)
            tail_m   = np.zeros(n)
            for j in range(n):
                d = pos - arr[j]
                if 0 < d < tail_len:
                    tail_m[j] = (1 - d / tail_len) ** 2 * amp * 0.5
            r, g, b  = palette(i / max(1, n_comets))
            total    = np.clip(m + tail_m, 0, 1)
            px[:, 0] = np.clip(px[:, 0] + r * total, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * total, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * total, 0, 255)
        return px


class FinalSparks(BaseEffect):
    name     = "FINAL\nSPARKS"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0 * speed)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        rng        = np.random.RandomState(beat_count)
        # Very few, slow sparks that flicker
        n_sparks   = 3
        arr        = np.arange(n, dtype=float)
        sig        = n * 0.007
        for i in range(n_sparks):
            pos    = rng.randint(0, n)
            amp    = max(0.0, 1.0 - beat_phase * 3)
            flicker = abs(math.sin(t * 7.0 + i * 2.3)) * amp
            m       = _gauss(arr, float(pos), sig) * flicker
            r, g, b = palette(rng.uniform(0, 1))
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class SlowDissolve(BaseEffect):
    name     = "SLOW\nDISSOLVE"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.arange(n, dtype=float)
        # Deterministic noise texture that fades overall
        grain     = (np.sin(arr * 0.31 + t * 0.7) * 0.5 + 0.5) * \
                    (np.cos(arr * 0.17 + t * 0.5) * 0.5 + 0.5)
        fade      = (1.0 - bar_phase) ** 2
        colors    = np.array([palette(arr[i] / n) for i in range(n)], dtype=float)
        mask      = grain * fade * 0.8
        px[:, 0]  = np.clip(colors[:, 0] * mask, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * mask, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * mask, 0, 255)
        return px


class AshFall(BaseEffect):
    name     = "ASH\nFALL"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        sig  = max(1.0, n * 0.006)
        rng  = np.random.RandomState(31)
        n_ash = max(4, min(30, n // 60))
        for i in range(n_ash):
            start = rng.uniform(0, n)
            rate  = rng.uniform(0.1, 0.4)
            pos   = (start + t * rate * speed * 6.0) % n
            # dims as it reaches end of strip
            amp   = max(0.0, 1.0 - (pos / n) ** 1.5) * 0.7
            m     = _gauss(arr, pos, sig) * amp
            r, g, b = palette(i / n_ash * 0.3)  # dim warm colors
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class LastSpark(BaseEffect):
    name     = "LAST\nSPARK"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        n_segs = max(1, n // 300)
        beats  = t * bpm / 60.0 * speed
        cycle  = (beats * 1.2 / n_segs) % 2.0
        pos   = cycle * n if cycle < 1.0 else (2.0 - cycle) * n
        # dims over time
        bar_phase = (beats / 4.0) % 1.0
        amp   = (1.0 - bar_phase * 0.8) * 0.9
        arr   = np.arange(n, dtype=float)
        sig   = max(1.5, n * 0.01)
        m     = _gauss(arr, pos, sig) * amp
        r, g, b = palette(0.07)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class SlowBurn(BaseEffect):
    name     = "SLOW\nBURN"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.linspace(0.0, 1.0, n)
        # embers: warm, randomly flickering low-level glow
        ember = (np.sin(arr * 4.1 + t * speed * 0.5) * 0.5 + 0.5) * \
                (np.cos(arr * 2.3 - t * speed * 0.3) * 0.5 + 0.5)
        # gradually fading over time
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        fade  = (1.0 - bar_phase) * 0.6
        colors = np.array([palette(arr[i] * 0.25) for i in range(n)], dtype=float)  # warm end
        px[:, 0] = np.clip(colors[:, 0] * ember * fade, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * ember * fade, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * ember * fade, 0, 255)
        return px


class FadeCenter(BaseEffect):
    name     = "FADE\nCENTER"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        # light collapses inward toward center then dims
        dist_center = np.abs(arr - 0.5) * 2.0  # 0 at center, 1 at edges
        collapse    = np.clip(1.0 - dist_center + bar_phase * 1.5 - 1.0, 0.0, 1.0)
        fade        = (1.0 - bar_phase) ** 1.2
        colors      = np.array([palette(arr[i]) for i in range(n)], dtype=float)
        mask        = collapse * fade
        px[:, 0]    = np.clip(colors[:, 0] * mask, 0, 255)
        px[:, 1]    = np.clip(colors[:, 1] * mask, 0, 255)
        px[:, 2]    = np.clip(colors[:, 2] * mask, 0, 255)
        return px


class StripDown(BaseEffect):
    name     = "STRIP\nDOWN"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        # strip turns off from right to left
        lit_end   = max(0, int(n * (1.0 - bar_phase)))
        if lit_end > 0:
            colors = np.array([palette(i / n) for i in range(n)], dtype=float)
            amp    = 0.7
            px[:lit_end, 0] = np.clip(colors[:lit_end, 0] * amp, 0, 255)
            px[:lit_end, 1] = np.clip(colors[:lit_end, 1] * amp, 0, 255)
            px[:lit_end, 2] = np.clip(colors[:lit_end, 2] * amp, 0, 255)
        return px


class NightOut(BaseEffect):
    name     = "NIGHT\nOUT"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        # slow overall fade with occasional faint flickers
        amp = (1.0 - bar_phase) ** 1.5 * 0.7
        arr = np.linspace(0.0, 1.0, n)
        flicker = 1.0 + (abs(math.sin(t * 3.7 * speed)) - 0.5) * 0.15
        colors  = np.array([palette(arr[i]) for i in range(n)], dtype=float)
        px[:, 0] = np.clip(colors[:, 0] * amp * flicker, 0, 255)
        px[:, 1] = np.clip(colors[:, 1] * amp * flicker, 0, 255)
        px[:, 2] = np.clip(colors[:, 2] * amp * flicker, 0, 255)
        return px


class WaveGoodbye(BaseEffect):
    name     = "WAVE\nBYE"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        pos   = (beats * 2.0) % n
        arr   = np.arange(n, dtype=float)
        sig   = max(3.0, n * 0.035)
        # wave rolls across; strip goes dark behind it
        m      = _gauss(arr, pos, sig)
        # behind the wave: already dark
        behind = np.where(arr < pos, max(0.0, 1.0 - pos / n) * 0.15, 0.0)
        total  = np.clip(m + behind, 0, 1)
        r, g, b = palette(pos / n)
        px[:, 0] = np.clip(r * total, 0, 255)
        px[:, 1] = np.clip(g * total, 0, 255)
        px[:, 2] = np.clip(b * total, 0, 255)
        return px


class CrystalFade(BaseEffect):
    name     = "CRYSTAL\nFADE"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px        = np.zeros((n, 3), dtype=np.uint8)
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr       = np.linspace(0.0, 1.0, n)
        # clean uniform fade across the whole strip
        amp       = (1.0 - bar_phase) ** 2
        colors    = np.array([palette(arr[i] * 0.7 + 0.15) for i in range(n)], dtype=float)
        px[:, 0]  = np.clip(colors[:, 0] * amp, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * amp, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * amp, 0, 255)
        return px


class FinalBreath(BaseEffect):
    name     = "FINAL\nBREATH"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        # one very slow breath then fades to black
        phase = min(1.0, t * speed * 0.04)  # climbs to 1 and stays
        amp   = (math.sin(phase * math.pi)) ** 2 * 0.85
        arr   = np.arange(n, dtype=float)
        sig   = n * 0.2
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(0.5)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class EchoExtinct(BaseEffect):
    name     = "ECHO\nEXTINCT"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        bar_phase  = (t * bpm / 60.0 * speed / 4.0) % 1.0
        arr        = np.arange(n, dtype=float)
        center     = n // 2
        bar_amp    = (1.0 - bar_phase) ** 1.5
        n_echoes   = 4
        for e in range(n_echoes):
            delay  = e / n_echoes
            ph     = (beat_phase - delay) % 1.0
            radius = ph * center * 0.6
            amp    = max(0.0, 1.0 - ph) / (e + 1) * bar_amp
            sig    = max(1.5, n * 0.015)
            for sign in (-1, 1):
                pos     = center + sign * radius
                m       = _gauss(arr, pos, sig) * amp
                r, g, b = palette(e / n_echoes)
                px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class FinalExpansion(BaseEffect):
    """Single slow ring expands from center to edges over 4 bars, then fades."""
    name     = "FINAL\nEXPAND"
    category = "outro"

    def render(self, t, bpm, n, palette, speed):
        bar_phase = (t * bpm / 60.0 * speed / 4.0) % 1.0
        center    = n * 0.5
        radius    = bar_phase * center * 1.05          # reaches edge at bar end
        fade_out  = (1.0 - bar_phase) ** 1.4
        arr       = np.arange(n, dtype=np.float32)
        dist      = np.abs(arr - center)
        ring_dist = np.abs(dist - radius)
        sig       = max(2.0, n * 0.018)
        envelope  = np.exp(-0.5 * (ring_dist / sig) ** 2).astype(np.float32)
        # trailing glow behind the ring
        behind    = np.where(dist < radius, np.clip(1.0 - ring_dist / (sig * 3), 0, 1) * 0.3, 0.0).astype(np.float32)
        total     = np.clip((envelope + behind) * fade_out, 0.0, 1.0)
        hue       = (dist / center * 0.6 + 0.2) % 1.0
        colors    = np.array([palette(float(hue[i])) for i in range(n)], dtype=np.float32)
        px        = np.zeros((n, 3), dtype=np.uint8)
        px[:, 0]  = np.clip(colors[:, 0] * total, 0, 255)
        px[:, 1]  = np.clip(colors[:, 1] * total, 0, 255)
        px[:, 2]  = np.clip(colors[:, 2] * total, 0, 255)
        return px


EFFECTS = [TrailingOff, FinalSparks, AshFall, SlowDissolve, EchoExtinct, FinalExpansion]
# Removed: NightOut (near-dup FadeCenter), FadeCenter (generic collapse fade)
