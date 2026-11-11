"""Globe (glorb) generative renderer.

Renders sphere-aware lighting patterns directly in 3D space.
Completely independent of the existing strip-based effect system.

Usage:
    renderer = GlobeRenderer(n_leds=120)
    pixels = renderer.render(t, bpm, params, beat_phase)
    # pixels: (n, 3) uint8 array in physical LED order
"""

import math
import numpy as np

_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _sphere_points(n: int) -> np.ndarray:
    """Fibonacci sphere: (n, 3) float32 in physical LED order (index 0..n-1)."""
    pts = np.zeros((n, 3), dtype=np.float32)
    for i in range(n):
        y = 1.0 - (2.0 * i + 1.0) / n
        r = math.sqrt(max(0.0, 1.0 - y * y))
        theta = _GOLDEN_ANGLE * i
        pts[i] = (math.cos(theta) * r, y, math.sin(theta) * r)
    return pts


class GlobeRenderer:
    """Renders generative sphere patterns. No dependency on existing effects."""

    def __init__(self, n_leds: int = 120):
        self._n   = n_leds
        self._pts = _sphere_points(n_leds)   # (n, 3) — physical LED coordinates
        # Precompute lat/lon for each LED
        self._y   = self._pts[:, 1]                              # latitude: -1..+1
        self._phi = np.arctan2(self._pts[:, 2], self._pts[:, 0]) # longitude: -π..+π

    def render(self, t: float, bpm: float, params: dict,
               beat_phase: float = 0.0) -> np.ndarray:
        """
        Render one frame.

        t           — wall-clock seconds (track position)
        bpm         — current BPM
        params      — globe plan entry dict (pattern, color_primary, speed, …)
        beat_phase  — 0.0..1.0 position within current beat (for beat_sync pulses)

        Returns (n, 3) uint8 array.
        """
        n       = self._n
        y       = self._y
        phi     = self._phi
        speed   = float(params.get("speed", 1.0))
        bright  = float(params.get("brightness", 0.5))
        cov     = float(params.get("coverage", 0.6))
        size    = float(params.get("size", 0.2))
        tail    = float(params.get("tail", 0.0))
        pattern = params.get("pattern", "solid_breathe")
        sync    = params.get("beat_sync", "off")

        c1 = np.array(params.get("color_primary",   [200, 100, 20]),  dtype=np.float32)
        c2 = np.array(params.get("color_secondary",  [10,  10, 60]),   dtype=np.float32)

        beats = t * bpm / 60.0

        # ── Beat sync pulse multiplier ──────────────────────────────────────
        pulse = 1.0
        if sync == "beat":
            pulse = max(0.0, math.cos(beat_phase * math.tau)) ** 2
        elif sync == "2beat":
            pulse = max(0.0, math.cos(beat_phase * math.pi)) ** 2
        elif sync == "bar":
            bar_phase = (beats % 4) / 4.0
            pulse = max(0.0, math.cos(bar_phase * math.tau)) ** 2

        # ── Pattern intensity field  (0..1 per LED) ─────────────────────────
        intensity = np.zeros(n, dtype=np.float32)

        if pattern == "solid_breathe":
            breath = 0.5 + 0.5 * math.sin(beats * speed * math.tau)
            intensity[:] = breath

        elif pattern == "pole_pulse":
            # Concentric rings from north pole, expanding outward over time
            pos   = (beats * speed * 0.5) % 1.0          # 0=pole, 1=south
            dist  = (1.0 - y) / 2.0                       # 0 at north, 1 at south
            sigma = size * 0.5
            phase = (dist - pos) % 1.0
            intensity = np.exp(-(phase ** 2) / (2 * sigma ** 2)).astype(np.float32)

        elif pattern == "equator_ring":
            # Band around equator, oscillates in width
            width = size * (1.0 + 0.3 * math.sin(beats * speed * 0.5))
            intensity = np.exp(-(y ** 2) / (2 * width ** 2)).astype(np.float32)
            # Slow rotation modulates via longitude
            rot = beats * speed * 0.3
            lon_mod = 0.5 + 0.5 * np.cos(phi - rot)
            intensity = intensity * (0.4 + 0.6 * lon_mod)

        elif pattern == "latitude_sweep":
            # Band traveling from south to north (y: -1 → +1)
            pos   = -1.0 + ((beats * speed * 0.4) % 2.0)   # sweeps -1..+1 repeatedly
            sigma = size * 0.6
            intensity = np.exp(-((y - pos) ** 2) / (2 * sigma ** 2)).astype(np.float32)

        elif pattern == "meridian_sweep":
            # Vertical slice rotating around the sphere
            target = (beats * speed * 0.5 % 1.0) * math.tau - math.pi
            d_phi  = np.abs(((phi - target + math.pi) % math.tau) - math.pi)
            sigma  = size * math.pi
            intensity = np.exp(-(d_phi ** 2) / (2 * sigma ** 2)).astype(np.float32)

        elif pattern == "hemisphere":
            # Top half c1, bottom half c2 — threshold oscillates slightly
            threshold = math.sin(beats * speed * 0.3) * 0.15
            intensity = (y > threshold).astype(np.float32)

        elif pattern == "scatter_burst":
            # Seeded random dots that pulse on beat
            rng   = np.random.default_rng(int(beats * 2) & 0xFFFF)
            n_pts = max(3, int(cov * n * 0.4))
            src_y   = rng.uniform(-1, 1,     size=n_pts).astype(np.float32)
            src_phi = rng.uniform(-math.pi, math.pi, size=n_pts).astype(np.float32)
            for sy, sp in zip(src_y, src_phi):
                dy   = y - sy
                dphi = np.abs(((phi - sp + math.pi) % math.tau) - math.pi)
                dist = np.sqrt(dy ** 2 + dphi ** 2)
                sigma = size * 0.8
                intensity += np.exp(-(dist ** 2) / (2 * sigma ** 2))
            np.clip(intensity, 0, 1, out=intensity)

        elif pattern == "random_dots":
            # Fixed random positions, pulsing gently
            rng   = np.random.default_rng(42)   # fixed seed = stable dots
            n_pts = max(2, int(cov * n * 0.3))
            src_y   = rng.uniform(-1, 1,     size=n_pts).astype(np.float32)
            src_phi = rng.uniform(-math.pi, math.pi, size=n_pts).astype(np.float32)
            for sy, sp in zip(src_y, src_phi):
                dy   = y - sy
                dphi = np.abs(((phi - sp + math.pi) % math.tau) - math.pi)
                dist = np.sqrt(dy ** 2 + dphi ** 2)
                sigma = size
                intensity += np.exp(-(dist ** 2) / (2 * sigma ** 2))
            breath = 0.5 + 0.5 * math.sin(beats * speed * 0.5)
            intensity *= breath
            np.clip(intensity, 0, 1, out=intensity)

        else:
            # Unknown pattern — fallback to solid breathe
            intensity[:] = 0.5 + 0.5 * math.sin(beats * speed * 0.3)

        # ── Coverage mask — cut LEDs below threshold to black ───────────────
        threshold = 1.0 - cov
        intensity = np.where(intensity > threshold,
                             intensity,
                             np.zeros_like(intensity)).astype(np.float32)

        # ── Apply beat sync pulse ────────────────────────────────────────────
        intensity *= float(pulse)

        # ── Blend colors ────────────────────────────────────────────────────
        # intensity → lerp between c2 (dark/secondary) and c1 (primary)
        i3 = intensity[:, np.newaxis]           # (n, 1)
        pixels = c2[np.newaxis, :] + i3 * (c1[np.newaxis, :] - c2[np.newaxis, :])

        # ── Master brightness ────────────────────────────────────────────────
        pixels *= bright

        return np.clip(pixels, 0, 255).astype(np.uint8)
