"""CHORUS effects — full brightness, full strip, beat-locked impact."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap, _palette_array, _tail_mask
from .verse import StrobeSections


class DualLaserChase(BaseEffect):
    name     = "DUAL\nLASER"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        beats = t * bpm / 60.0 * speed
        arr   = np.arange(n, dtype=float)
        sig   = max(8.0, n * 0.03)
        # 1 lap per 4 beats — fast racing pair
        p1 = (beats / 4.0 % 1.0) * n
        p2 = ((beats / 4.0 + 0.5) % 1.0) * n
        m1 = _gauss_wrap(arr, p1, sig, n)
        m2 = _gauss_wrap(arr, p2, sig, n)
        r,  g,  b  = palette(0.1)
        r2, g2, b2 = palette(0.65)
        px[:, 0] = np.clip(r * m1 + r2 * m2, 0, 255)
        px[:, 1] = np.clip(g * m1 + g2 * m2, 0, 255)
        px[:, 2] = np.clip(b * m1 + b2 * m2, 0, 255)
        return px


class ImpactWaves(BaseEffect):
    name     = "IMPACT\nWAVES"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        arr    = np.arange(n, dtype=float)
        center = n // 2
        sig    = max(6.0, n * 0.025)
        # 1 wave per 2 beats — punchy and frequent
        for offset, col_t in ((0.0, 0.05), (1.0, 0.6)):
            phase  = ((beats + offset) / 2.0) % 1.0
            radius = phase * (n // 2)
            amp    = (1.0 - phase) ** 0.4
            if radius < 2.0:
                continue
            r, g, b = palette(col_t)
            for sign in (-1, 1):
                pos = center + sign * radius
                m   = _gauss(arr, pos, sig) * amp
                px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class ScatterBurst(BaseEffect):
    name     = "SCATTER\nBURST"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        beat_phase = beats % 1.0
        arr        = np.arange(n, dtype=float)
        center     = n // 2
        amp        = max(0.0, 1.0 - beat_phase * 1.6) ** 0.5
        if amp < 0.01:
            return px
        block  = int(beats / 2)
        rng    = np.random.RandomState(block + 42)
        sig    = max(12.0, n * 0.022)   # wider blobs for visibility
        for i in range(8):
            direction = rng.choice([-1, 1])
            spd_px    = rng.uniform(0.3, 0.9) * n * 0.5
            pos       = center + direction * beat_phase * spd_px
            m         = _gauss(arr, pos, sig) * amp
            r, g, b   = palette(rng.uniform(0, 1))
            px[:, 0]  = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1]  = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2]  = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class SegmentImplosion(BaseEffect):
    name     = "IMPLO\nSION"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        arr        = np.arange(n, dtype=float)
        beats      = t * bpm / 60.0 * speed
        beat_phase = beats % 1.0
        beat_count = int(beats)
        seg_size   = n // 5
        rng        = np.random.RandomState(beat_count + 13)
        radius     = seg_size * 0.5 * (1.0 - beat_phase)
        amp        = beat_phase ** 0.3 * max(0.0, 1.0 - max(0.0, beat_phase - 0.75) / 0.25)
        sig        = max(3.0, seg_size * 0.04)
        for seg in range(5):
            center  = seg * seg_size + seg_size // 2
            r, g, b = palette(rng.uniform(0, 1))
            for sign in (-1, 1):
                pos = center + sign * radius
                m   = _gauss(arr, pos, sig) * amp
                px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
            if beat_phase > 0.75:
                flash_amp = ((beat_phase - 0.75) / 0.25) ** 0.4
                flash_sig = max(2.0, seg_size * 0.04)
                m = _gauss(arr, float(center), flash_sig) * flash_amp
                fr, fg, fb = palette(rng.uniform(0, 0.3))
                px[:, 0] = np.clip(px[:, 0] + fr * m, 0, 255)
                px[:, 1] = np.clip(px[:, 1] + fg * m, 0, 255)
                px[:, 2] = np.clip(px[:, 2] + fb * m, 0, 255)
        return px


class AlternateFlash(BaseEffect):
    """Sections alternate hard on beat — full brightness, slow decay."""
    name     = "ALT\nFLASH"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_phase = beats % 1.0
        beat_count = int(beats)
        amp        = max(0.0, 1.0 - beat_phase * 1.8) ** 0.3
        px         = np.zeros((n, 3), dtype=np.uint8)
        n_sects    = 4
        sect_w     = n // n_sects
        parity     = beat_count % 2
        r, g, b    = palette(beat_count * 0.17 % 1.0)
        for s in range(n_sects):
            if s % 2 == parity:
                start = s * sect_w
                end   = min(n, start + sect_w)
                px[start:end, 0] = int(r * amp)
                px[start:end, 1] = int(g * amp)
                px[start:end, 2] = int(b * amp)
        return px


class BeatStrobe(BaseEffect):
    """Full strip slam on beat — primary palette color, alternates with secondary."""
    name     = "BEAT\nSTROBE"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_phase = math.fmod(beats, 1.0)
        beat_count = int(beats)
        px         = np.zeros((n, 3), dtype=np.uint8)
        if beat_phase < 0.30:
            amp = (1.0 - beat_phase / 0.30) ** 0.5
            col_t   = 0.0 if beat_count % 2 == 0 else 0.55
            r, g, b = palette(col_t)
            px[:, 0] = int(r * amp)
            px[:, 1] = int(g * amp)
            px[:, 2] = int(b * amp)
        return px


class BlockStrobe(BaseEffect):
    """Wide bright blocks slam on beat — more blocks, wider, full brightness."""
    name     = "BLOCK\nSTROBE"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_phase = beats % 1.0
        beat_count = int(beats)
        amp        = max(0.0, 1.0 - beat_phase * 2.5) ** 0.3
        px         = np.zeros((n, 3), dtype=np.uint8)
        if amp < 0.01:
            return px
        block_w = max(8, n // 6)   # wider blocks
        rng     = np.random.RandomState(beat_count + 5)
        n_blks  = 5
        for i in range(n_blks):
            pos     = rng.randint(0, max(1, n - block_w))
            r, g, b = palette(rng.uniform(0, 1))
            px[pos:pos + block_w, 0] = int(r * amp)
            px[pos:pos + block_w, 1] = int(g * amp)
            px[pos:pos + block_w, 2] = int(b * amp)
        return px


class FireEffect(BaseEffect):
    """Burning fire with fast flicker — maximum heat."""
    name     = "FIRE\nEFFECT"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        arr  = np.arange(n, dtype=np.float32) / n
        s    = max(speed, 0.6)
        base = (1.0 - arr) ** 1.1
        flicker = (
            np.sin(arr * 15.0 + t * s * 8.3).astype(np.float32) * 0.16 +
            np.sin(arr *  8.0 - t * s * 13.1).astype(np.float32) * 0.13 +
            np.sin(arr * 24.0 + t * s *  6.7).astype(np.float32) * 0.10 +
            np.sin(arr *  4.5 - t * s * 19.3).astype(np.float32) * 0.08
        )
        heat   = np.clip(base + flicker, 0.0, 1.0)
        colors = _palette_array(palette, heat)
        return np.clip(colors, 0, 255).astype(np.uint8)


class TidalFlood(BaseEffect):
    """Massive wave surging end-to-end every 2 beats — leaves full bright wake."""
    name     = "TIDAL\nFLOOD"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.arange(n, dtype=float)
        beats = t * bpm / 60.0 * speed
        sig   = max(20.0, n * 0.08)
        # 1 full crossing per 2 beats
        pos   = (beats * 0.5 % 1.0) * n
        dist  = np.minimum(np.abs(arr - pos), n - np.abs(arr - pos))
        front = np.exp(-dist ** 2 / (2 * sig ** 2))
        wake_dist = (arr - pos) % n
        wake  = np.exp(-wake_dist / (n * 0.15)) * 0.70   # brighter wake
        m     = np.clip(front + wake, 0, 1)
        hue   = (beats * 0.12) % 1.0
        r, g, b = palette(hue)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class ChromaRush(BaseEffect):
    """Full-strip palette gradient rushing at high speed — two layers."""
    name     = "CHROMA\nRUSH"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats  = t * bpm / 60.0 * speed
        lut    = _palette_array(palette, np.linspace(0, 1, n))
        # Fast: scroll 3x faster than before
        shift1 = int((beats * 0.55 % 1.0) * n) % n
        layer1 = np.roll(lut, shift1, axis=0).astype(np.float32)
        shift2 = int((1.0 - beats * 0.22 % 1.0) * n) % n
        layer2 = np.roll(lut, shift2, axis=0).astype(np.float32)
        return np.clip((layer1 * 0.75 + layer2 * 0.55), 0, 255).astype(np.uint8)


class PlasmaField(BaseEffect):
    """4-wave sine plasma — full psychedelic colour field."""
    name     = "PLASMA\nFIELD"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        ts  = float(t) * float(max(speed, 0.5)) * 1.2
        x   = np.linspace(0.0, math.pi * 4, n, dtype=np.float32)
        plasma = (
            np.sin(x          + np.float32(ts * 1.31)) +
            np.sin(x * 2.09   - np.float32(ts * 0.89)) +
            np.sin(x * 0.73   + np.float32(ts * 1.73)) +
            np.sin(x * 3.14   - np.float32(ts * 0.51))
        ) * np.float32(0.25)
        hue = (plasma + np.float32(1.0)) * np.float32(0.5)
        return _palette_array(palette, hue).clip(0, 255).astype(np.uint8)


class SegmentDisco(BaseEffect):
    """5 segments snap to full-brightness solid colors every beat."""
    name     = "SEGMENT\nDISCO"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_count = int(beats)
        beat_phase = beats % 1.0
        # Hold for 60% of beat then quick cut
        amp        = max(0.0, 1.0 - max(0.0, beat_phase - 0.60) * 4.0) ** 0.4
        canvas     = np.zeros((n, 3), dtype=np.float32)
        seg_w      = n // 5
        rng        = np.random.RandomState(beat_count + 77)
        hues       = rng.uniform(0, 1, 5)
        for s in range(5):
            r, g, b = palette(float(hues[s]))
            lo, hi  = s * seg_w, min(n, (s + 1) * seg_w)
            canvas[lo:hi, 0] = r * amp
            canvas[lo:hi, 1] = g * amp
            canvas[lo:hi, 2] = b * amp
        return np.clip(canvas, 0, 255).astype(np.uint8)


class ColorSlam(BaseEffect):
    """Every beat: entire strip slams to a vivid solid color — maximum impact."""
    name     = "COLOR\nSLAM"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_phase = beats % 1.0
        beat_count = int(beats)
        # Hard on, exponential decay over 40% of beat
        amp = math.exp(-beat_phase * 5.0)
        if amp < 0.02:
            return np.zeros((n, 3), dtype=np.uint8)
        r, g, b = palette(beat_count * 0.23 % 1.0)
        px = np.empty((n, 3), dtype=np.uint8)
        px[:, 0] = int(r * amp)
        px[:, 1] = int(g * amp)
        px[:, 2] = int(b * amp)
        return px


class RocketRace(BaseEffect):
    """3 bright rockets race end-to-end on every beat at different speeds."""
    name     = "ROCKET\nRACE"
    category = "chorus"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.arange(n, dtype=float)
        beats = t * bpm / 60.0 * speed
        sig   = max(6.0, n * 0.02)
        speeds = [1.0, 1.5, 2.2]   # different lap rates
        cols   = [0.05, 0.40, 0.75]
        for spd, col_t in zip(speeds, cols):
            pos     = (beats * spd % 1.0) * n
            m       = _gauss_wrap(arr, pos, sig, n)
            r, g, b = palette(col_t)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class ChorusImpactFlash(BaseEffect):
    """Hard strobe burst timed from chorus hit — always maximum brightness."""
    name     = "IMPACT\nFLASH"
    category = "chorus"

    def __init__(self):
        self._t0 = None

    def render(self, t, bpm, n, palette, speed):
        if self._t0 is None:
            self._t0 = t
        lt    = t - self._t0
        hz    = bpm / 60.0 * speed * 4.0
        phase = (lt * hz) % 1.0
        pulse = int(lt * hz)
        px    = np.zeros((n, 3), dtype=np.uint8)
        if phase < 0.35:
            amp = 1.0 - phase / 0.35
            if pulse % 2 == 0:
                r, g, b = palette(0.0)
                px[:, 0] = int(r * amp)
                px[:, 1] = int(g * amp)
                px[:, 2] = int(b * amp)
            else:
                r, g, b = palette(0.55)
                px[:, 0] = int(r * amp)
                px[:, 1] = int(g * amp)
                px[:, 2] = int(b * amp)
        return px


EFFECTS = [
    DualLaserChase, ImpactWaves, ScatterBurst, SegmentImplosion,
    AlternateFlash, BeatStrobe, BlockStrobe,
    FireEffect, TidalFlood, ChromaRush,
    PlasmaField, SegmentDisco, ColorSlam, RocketRace,
]
