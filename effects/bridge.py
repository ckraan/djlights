"""BRIDGE effects — atmospheric, floating, minimal, slow breathing, spacious."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap

# Shared effects imported from other categories
from .down  import SingleBreath, LongBreath, SparseDrift, EchoPulse, MemoryFade
from .intro import NightSky, FogDrift, TwilightWave, DualBreath, PulseRipple, GlowCenter


class CrossfadeSpots(BaseEffect):
    name     = "CROSS\nSPOTS"
    category = "bridge"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        arr    = np.arange(n, dtype=float)
        n_spots = 3
        for i in range(n_spots):
            offset = i / n_spots
            phase  = (beats * 0.25 * n_segs + offset) % 1.0
            pos  = n * 0.2 + n * 0.6 * (math.sin(phase * 2 * math.pi) * 0.5 + 0.5)
            sig  = n * 0.06
            xfade = abs(math.sin(beats * 0.5 + i * 1.2))
            m    = _gauss(arr, pos, sig) * xfade
            r, g, b = palette((i / n_spots + beats * 0.05) % 1.0)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class RotatingBeam(BaseEffect):
    name     = "ROTATING\nBEAM"
    category = "bridge"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        arr    = np.arange(n, dtype=float)
        phase  = (beats * 0.5 * n_segs) % 1.0
        eased  = math.sin(phase * math.pi) ** 2
        pos    = eased * (n - 1)
        sig    = n * 0.018
        halo   = n * 0.06
        m      = _gauss(arr, pos, sig)
        hm     = _gauss(arr, pos, halo) * 0.2
        r, g, b = palette(eased * 0.8)
        total   = np.clip(m + hm, 0, 1)
        px[:, 0] = np.clip(r * total, 0, 255)
        px[:, 1] = np.clip(g * total, 0, 255)
        px[:, 2] = np.clip(b * total, 0, 255)
        return px


class SplitComets(BaseEffect):
    name     = "SPLIT\nCOMETS"
    category = "bridge"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        arr    = np.arange(n, dtype=float)
        center = n // 2
        phase  = (beats * 0.2 * n_segs) % 1.0
        for sign, cp in ((-1, 0.1), (1, 0.7)):
            pos  = center + sign * phase * n * 0.5
            sig  = n * 0.014
            m    = _gauss(arr, pos, sig)
            tail_len = n * 0.10
            tail_m   = np.zeros(n)
            for i in range(n):
                d = (arr[i] - pos) * (-sign)
                if 0 < d < tail_len:
                    tail_m[i] = ((tail_len - d) / tail_len) ** 2 * 0.5
            r, g, b  = palette(cp)
            total    = np.clip(m + tail_m, 0, 1)
            px[:, 0] = np.clip(px[:, 0] + r * total, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * total, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * total, 0, 255)
        return px


class ColdBreath(BaseEffect):
    name     = "COLD\nBREATH"
    category = "bridge"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        phase = (beats * 0.2) % 1.0
        amp   = (math.sin(phase * 2 * math.pi) * 0.5 + 0.5) ** 3
        arr   = np.arange(n, dtype=float)
        sig   = n * (0.12 + 0.08 * amp)
        m     = _gauss(arr, n * 0.5, sig) * amp
        r, g, b = palette(0.6)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


EFFECTS = [
    CrossfadeSpots, RotatingBeam, SplitComets, ColdBreath,
    SingleBreath, LongBreath, SparseDrift, EchoPulse, MemoryFade,
    NightSky, FogDrift, TwilightWave, DualBreath, PulseRipple, GlowCenter,
]
