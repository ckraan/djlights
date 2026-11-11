"""VERSE effects — rhythmic, medium energy."""
import numpy as np
import math
from .base import BaseEffect, _gauss, _gauss_wrap, _palette_array, _tail_mask


class LaserScan(BaseEffect):
    name     = "LASER\nSCAN"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0
        n_segs = max(1, n // 300)
        # Two lasers, one per beat direction, sync to bar
        pos1  = (beats * speed * 2.0 * n_segs) % n
        pos2  = n - 1 - (beats * speed * 1.5 * n_segs % n)
        arr   = np.arange(n, dtype=float)
        sig   = max(1.0, n * 0.007)
        m1    = _gauss(arr, pos1, sig)
        m2    = _gauss(arr, pos2, sig) * 0.7
        r, g, b   = palette(0.1)
        r2, g2, b2 = palette(0.7)
        px[:, 0] = np.clip(r * m1 + r2 * m2, 0, 255)
        px[:, 1] = np.clip(g * m1 + g2 * m2, 0, 255)
        px[:, 2] = np.clip(b * m1 + b2 * m2, 0, 255)
        return px


class StrobeSections(BaseEffect):
    name     = "STROBE\nSECTIONS"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        beat_count = int(t * bpm / 60.0 * speed)
        n_sections = 4
        sec_size   = n // n_sections
        active_sec = beat_count % n_sections
        fade       = max(0.0, 1.0 - beat_phase * 3)
        start = active_sec * sec_size
        end   = start + sec_size
        if fade > 0:
            r, g, b = palette(active_sec / n_sections)
            px[start:end, 0] = int(r * fade)
            px[start:end, 1] = int(g * fade)
            px[start:end, 2] = int(b * fade)
        return px


class PulseDuo(BaseEffect):
    name     = "PULSE\nDUO"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        phase = (t * bpm / 60.0 * speed) % 1.0
        arr   = np.arange(n, dtype=float)
        # Two pulses moving toward each other
        p1    = phase * (n // 2)
        p2    = n - 1 - phase * (n // 2)
        sig   = n * 0.04
        m1    = _gauss(arr, p1, sig)
        m2    = _gauss(arr, p2, sig)
        r, g, b   = palette(phase * 0.5)
        r2, g2, b2 = palette(0.5 + phase * 0.5)
        px[:, 0] = np.clip(r * m1 + r2 * m2, 0, 255)
        px[:, 1] = np.clip(g * m1 + g2 * m2, 0, 255)
        px[:, 2] = np.clip(b * m1 + b2 * m2, 0, 255)
        return px


class MeteorShower(BaseEffect):
    """Stateful meteor: persistent decay buffer + random sparks with 10% stay-bright chance."""
    name     = "METEOR\nSHOWER"
    category = "verse"

    _TAIL_DECAY  = 0.84   # per-frame trail fade (~15 frames to near-zero at 30fps)
    _SPARK_DECAY = 0.68   # sparks fade faster
    _N_SPARKS    = 6      # sparks injected each frame near the head

    def __init__(self):
        self._buf   = None   # float32 (n, 3) — lingering trail
        self._sbuf  = None   # float32 (n, 3) — spark layer
        self._n     = 0

    def render(self, t, bpm, n, palette, speed):
        if n != self._n:
            self._buf  = np.zeros((n, 3), dtype=np.float32)
            self._sbuf = np.zeros((n, 3), dtype=np.float32)
            self._n    = n

        # --- Dim all pixels this frame (the "persistence" step) ---
        self._buf  *= self._TAIL_DECAY

        # Spark decay: each pixel independently — 10% chance it stays bright
        keep = (np.random.random(n) < 0.10).reshape(n, 1)
        self._sbuf *= np.where(keep, 1.0, self._SPARK_DECAY).astype(np.float32)

        # --- Head position: absolute traveling formula ---
        leds_per_beat = n / 4.0   # crosses full strip in 4 beats at speed=1
        pos = (t * bpm / 60.0 * speed * leds_per_beat) % n

        # Head: tight, very bright spot — use maximum so it always peaks at full color
        head_r, head_g, head_b = palette(0.92)
        head_sig = max(2.0, n * 0.007)
        arr  = np.arange(n, dtype=np.float32)
        dist = np.minimum((arr - pos) % n, (pos - arr) % n)
        head_m = np.exp(-(dist ** 2) / (2.0 * head_sig ** 2))

        self._buf[:, 0] = np.maximum(self._buf[:, 0], head_r * head_m * 255)
        self._buf[:, 1] = np.maximum(self._buf[:, 1], head_g * head_m * 255)
        self._buf[:, 2] = np.maximum(self._buf[:, 2], head_b * head_m * 255)

        # --- Sparks: random bright flicker pixels in the tail behind the head ---
        tail_len = max(3, int(n * 0.18))
        offsets  = np.random.randint(2, tail_len, size=self._N_SPARKS)
        for off in offsets:
            sp = int((pos - off) % n)
            br = np.random.uniform(0.4, 1.0)
            sr, sg, sb = palette(np.random.uniform(0.65, 1.0))
            self._sbuf[sp, 0] = min(255.0, sr * br * 255)
            self._sbuf[sp, 1] = min(255.0, sg * br * 255)
            self._sbuf[sp, 2] = min(255.0, sb * br * 255)

        return np.clip(self._buf + self._sbuf, 0, 255).astype(np.uint8)


class ScanLine(BaseEffect):
    name     = "SCAN\nLINE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        # Sine wave sweep — slows at ends like a scanner
        phase  = (beats / (2.0 * n_segs)) % 1.0
        norm   = math.sin(phase * math.pi)
        pos    = norm * (n - 1)
        arr    = np.arange(n, dtype=float)
        sig    = max(2.0, n * 0.015)
        m      = _gauss(arr, pos, sig)
        # Width halo
        halo_sig = n * 0.04
        halo_m   = _gauss(arr, pos, halo_sig) * 0.3
        r, g, b  = palette(norm)
        total    = np.clip(m + halo_m, 0, 1)
        px[:, 0] = np.clip(r * total, 0, 255)
        px[:, 1] = np.clip(g * total, 0, 255)
        px[:, 2] = np.clip(b * total, 0, 255)
        return px


class BeatFlash(BaseEffect):
    name     = "BEAT\nFLASH"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        amp        = max(0.0, 1.0 - beat_phase * 5)
        if amp > 0.01:
            beat_count = int(t * bpm / 60.0 * speed)
            r, g, b = palette(beat_count * 0.11 % 1.0)
            px[:, 0] = int(r * amp)
            px[:, 1] = int(g * amp)
            px[:, 2] = int(b * amp)
        return px


class TripleChase(BaseEffect):
    name     = "TRIPLE\nCHASE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px   = np.zeros((n, 3), dtype=np.uint8)
        arr  = np.arange(n, dtype=float)
        sig    = max(1.5, n * 0.008)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        for i in range(3):
            pos     = (beats * 2.5 * n_segs + i * n / 3.0) % n
            m       = _gauss_wrap(arr, pos, sig, n)
            r, g, b = palette(i / 3.0)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class RhythmBlocks(BaseEffect):
    name     = "RHYTHM\nBLOCKS"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0 * speed)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        n_blocks   = 6
        block_w    = n // n_blocks
        active     = beat_count % n_blocks
        amp        = max(0.0, 1.0 - beat_phase * 2.5)
        for b in range(n_blocks):
            if b == active and amp > 0.01:
                r, g, b2 = palette(b / n_blocks)
                s = b * block_w; e = min(n, s + block_w)
                px[s:e, 0] = int(r * amp)
                px[s:e, 1] = int(g * amp)
                px[s:e, 2] = int(b2 * amp)
        return px


class BounceBall(BaseEffect):
    name     = "BOUNCE\nBALL"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px     = np.zeros((n, 3), dtype=np.uint8)
        beats  = t * bpm / 60.0 * speed
        n_segs = max(1, n // 300)
        cycle  = (beats * 2.0 / n_segs) % 2.0
        pos   = cycle * n if cycle < 1.0 else (2.0 - cycle) * n
        arr   = np.arange(n, dtype=float)
        sig   = max(2.0, n * 0.018)
        m     = _gauss(arr, pos, sig)
        r, g, b = palette(pos / n)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class ColorPing(BaseEffect):
    name     = "COLOR\nPING"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_count = int(t * bpm / 60.0 * speed)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        n_segs     = max(1, n // 300)
        arr        = np.arange(n, dtype=float)
        sig        = max(2.0, n * 0.012)
        # alternating: even beats fire from left, odd from right
        if beat_count % 2 == 0:
            pos = beat_phase * n
        else:
            pos = n - beat_phase * n
        amp     = max(0.0, 1.0 - beat_phase * 1.2)
        m       = _gauss(arr, pos, sig) * amp
        r, g, b = palette(beat_count * 0.13 % 1.0)
        px[:, 0] = np.clip(r * m, 0, 255)
        px[:, 1] = np.clip(g * m, 0, 255)
        px[:, 2] = np.clip(b * m, 0, 255)
        return px


class ReverbDot(BaseEffect):
    name     = "REVERB\nDOT"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        arr        = np.arange(n, dtype=float)
        n_segs     = max(1, n // 300)
        pos        = (beats * 3.0 * n_segs) % n
        sig_head   = max(1.5, n * 0.008)
        m_head     = _gauss_wrap(arr, pos, sig_head, n)
        r, g, b    = palette(pos / n)
        px[:, 0]   = np.clip(r * m_head, 0, 255)
        px[:, 1]   = np.clip(g * m_head, 0, 255)
        px[:, 2]   = np.clip(b * m_head, 0, 255)
        # reverb echoes
        for e in range(1, 4):
            echo_pos = (pos - e * n * 0.08) % n
            amp      = 0.5 / e
            sig_e    = sig_head * (1.0 + e * 0.5)
            m_e      = _gauss_wrap(arr, echo_pos, sig_e, n) * amp
            px[:, 0] = np.clip(px[:, 0] + r * m_e, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m_e, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m_e, 0, 255)
        return px


class CrossPulse(BaseEffect):
    name     = "CROSS\nPULSE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        n_segs     = max(1, n // 300)
        arr        = np.arange(n, dtype=float)
        sig        = max(2.0, n * 0.018)
        p_l        = beat_phase * n
        p_r        = n - beat_phase * n
        amp        = 1.0 - beat_phase * 0.8
        r, g, b    = palette(0.1)
        r2, g2, b2 = palette(0.7)
        ml = _gauss(arr, p_l, sig) * amp
        mr = _gauss(arr, p_r, sig) * amp
        px[:, 0] = np.clip(r * ml + r2 * mr, 0, 255)
        px[:, 1] = np.clip(g * ml + g2 * mr, 0, 255)
        px[:, 2] = np.clip(b * ml + b2 * mr, 0, 255)
        return px


class HeartPulse(BaseEffect):
    name     = "HEART\nPULSE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beat_phase = (t * bpm / 60.0 * speed) % 1.0
        n_segs     = max(1, n // 300)
        arr        = np.arange(n, dtype=float)
        center     = n // 2
        radius     = beat_phase * (n // 2)
        amp        = (1.0 - beat_phase) ** 0.7
        sig        = max(2.0, n * 0.022)
        for sign in (-1, 1):
            pos     = center + sign * radius
            m       = _gauss(arr, pos, sig) * amp
            r, g, b = palette(beat_phase * 0.4)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class SliceWave(BaseEffect):
    name     = "SLICE\nWAVE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px       = np.zeros((n, 3), dtype=np.uint8)
        beats    = t * bpm / 60.0 * speed
        n_slices = 4
        slice_w  = n // n_slices
        for s in range(n_slices):
            phase = (beats * 0.5 + s / n_slices) % 1.0
            amp   = math.sin(phase * math.pi) ** 2
            start = s * slice_w; end = min(n, start + slice_w)
            r, g, b = palette(s / n_slices)
            px[start:end, 0] = int(r * amp)
            px[start:end, 1] = int(g * amp)
            px[start:end, 2] = int(b * amp)
        return px


class BeatStack(BaseEffect):
    name     = "BEAT\nSTACK"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        beats      = t * bpm / 60.0 * speed
        n_filled   = int(beats % 4)
        beat_phase = beats % 1.0
        fill_end   = min(n, int((n_filled + beat_phase) / 4.0 * n))
        if fill_end > 0:
            colors = _palette_array(palette, np.linspace(0.0, 1.0, fill_end))
            px[:fill_end, 0] = np.clip(colors[:, 0], 0, 255)
            px[:fill_end, 1] = np.clip(colors[:, 1], 0, 255)
            px[:fill_end, 2] = np.clip(colors[:, 2], 0, 255)
        return px


class OceanWave(BaseEffect):
    """Two soft sine waves traveling forward at slightly different speeds, overlapping
    to create a natural water-like flow across the full strip."""
    name     = "OCEAN\nWAVE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px    = np.zeros((n, 3), dtype=np.uint8)
        arr   = np.arange(n, dtype=float)
        beats = t * bpm / 60.0 * speed
        sig   = max(20.0, n * 0.10)
        for i, (rate, hue_shift, amp) in enumerate(((1.0, 0.0, 1.0), (0.65, 0.35, 0.55), (1.4, 0.65, 0.35))):
            pos = (beats * rate * 60.0) % n
            dist = np.minimum(np.abs(arr - pos), n - np.abs(arr - pos))
            m   = np.exp(-dist ** 2 / (2 * sig ** 2)) * amp
            hue = (hue_shift + beats * 0.03) % 1.0
            r, g, b = palette(hue)
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


class ColorRiver(BaseEffect):
    """Smooth palette gradient that flows continuously from one end to the other —
    like a river of color washing across the rig."""
    name     = "COLOR\nRIVER"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        beats  = t * bpm / 60.0 * speed
        offset = (beats * 0.12) % 1.0          # scroll position in palette space
        lut    = _palette_array(palette, np.linspace(0, 1, n))  # shape (n, 3)
        # Shift palette by scroll offset
        shift  = int(offset * n) % n
        px     = np.roll(lut, shift, axis=0).astype(np.uint8)
        return px


class TheaterChase(BaseEffect):
    """Classic marquee — every 3rd LED lit, rolling window advances every half-beat.
    Hard edges, no blur. Looks nothing like any gaussian-blob effect."""
    name     = "THEATER\nCHASE"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        beats      = t * bpm / 60.0 * speed
        beat_count = int(beats)
        offset     = int(beats * 2) % 3
        mask       = (np.arange(n, dtype=np.int32) % 3 == offset).astype(np.float32)
        r, g, b    = palette(beat_count * 0.13 % 1.0)
        px         = np.zeros((n, 3), dtype=np.uint8)
        px[:, 0]   = np.clip(r * mask, 0, 255)
        px[:, 1]   = np.clip(g * mask, 0, 255)
        px[:, 2]   = np.clip(b * mask, 0, 255)
        return px


class RainDrops(BaseEffect):
    """3-5 drops land at random strip positions each beat, splashing ring ripples outward.
    Impact flash + expanding rings — dynamic and spatially varied every beat."""
    name     = "RAIN\nDROPS"
    category = "verse"

    def render(self, t, bpm, n, palette, speed):
        px         = np.zeros((n, 3), dtype=np.uint8)
        arr        = np.arange(n, dtype=np.float32)
        beats      = t * bpm / 60.0 * speed
        beat_count = int(beats)
        beat_phase = beats % 1.0
        rng        = np.random.RandomState(beat_count + 55)
        n_drops    = rng.randint(3, 6)
        positions  = rng.randint(n // 10, n - n // 10, n_drops)
        hues       = rng.uniform(0, 1, n_drops)
        splash_sig = max(1.0, n * 0.005)
        ring_sig   = max(2.0, n * 0.012)
        for pos, hue in zip(positions, hues):
            r, g, b  = palette(float(hue))
            splash   = _gauss(arr, float(pos), splash_sig) * max(0.0, 1.0 - beat_phase * 7)
            radius   = beat_phase * n * 0.20
            ring_amp = (1.0 - beat_phase) ** 1.5 * 0.85
            m = splash
            if radius > 2.0:
                for sign in (-1, 1):
                    m = m + _gauss(arr, float(pos) + sign * radius, ring_sig) * ring_amp
            px[:, 0] = np.clip(px[:, 0] + r * m, 0, 255)
            px[:, 1] = np.clip(px[:, 1] + g * m, 0, 255)
            px[:, 2] = np.clip(px[:, 2] + b * m, 0, 255)
        return px


# Removed: TripleChase (near-dup of LaserScan), OceanWave (near-dup of FogDrift)
# BounceBall exists above in this file — included here
EFFECTS = [LaserScan, MeteorShower, RhythmBlocks, ColorRiver,
           BounceBall, TheaterChase, RainDrops]
