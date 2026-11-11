"""LED geometry — maps LED indices onto a canvas pixel grid.

Three geometry types:
  linear  — single horizontal strip
  snake   — multi-segment snake wiring (auto-reverses even rows)
  globe   — LEDs distributed around a circle / spiral
"""
import numpy as np
import math


class Geometry:
    type_name = "linear"

    @staticmethod
    def from_dict(d):
        if not d:
            return LinearGeometry()
        t = d.get("type", "linear")
        if t == "snake":
            return SnakeGeometry(d.get("segments", 5),
                                 d.get("leds_per_segment", 300),
                                 d.get("gap_px", 15))
        if t == "globe":
            return GlobeGeometry(d.get("led_count", 120),
                                 d.get("rotation_offset", 0),
                                 d.get("mapping", "sphere"),
                                 d.get("sort_axis", "y"),
                                 d.get("grid_w", 0),
                                 d.get("grid_h", 0))
        return LinearGeometry(d.get("led_count", 300))

    def n_leds(self) -> int:
        return 0

    def to_dict(self) -> dict:
        raise NotImplementedError

    def build_led_map(self, w: int, h: int) -> np.ndarray:
        """Return (h × w) int32 array.  value = LED index,  -1 = background."""
        return np.full((h, w), -1, dtype=np.int32)


# ── Linear ────────────────────────────────────────────────────────────────────

class LinearGeometry(Geometry):
    type_name = "linear"

    def __init__(self, led_count: int = 300):
        self._n = int(led_count)

    def n_leds(self):
        return self._n

    def to_dict(self):
        return {"type": "linear", "led_count": self._n}

    def build_led_map(self, w, h):
        m = np.full((h, w), -1, dtype=np.int32)
        n = self._n
        if n == 0 or w < 4 or h < 4:
            return m
        pad   = 10
        uw    = max(1, w - 2 * pad)
        lw    = max(1, uw // n)
        lh    = min(20, max(4, h // 3))
        cy    = h // 2
        y0, y1 = max(0, cy - lh // 2), min(h, cy + lh // 2 + 1)
        for i in range(n):
            x0 = pad + i * lw
            x1 = min(w, x0 + lw)
            if x0 >= w:
                break
            m[y0:y1, x0:x1] = i
        return m


# ── Snake ─────────────────────────────────────────────────────────────────────

class SnakeGeometry(Geometry):
    type_name = "snake"

    def __init__(self, segments: int = 5, leds_per_segment: int = 300,
                 gap_px: int = 15):
        self.segments          = int(segments)
        self.leds_per_segment  = int(leds_per_segment)
        self.gap_px            = int(gap_px)

    def n_leds(self):
        return self.segments * self.leds_per_segment

    def to_dict(self):
        return {"type": "snake",
                "segments": self.segments,
                "leds_per_segment": self.leds_per_segment,
                "gap_px": self.gap_px}

    def build_led_map(self, w, h):
        m   = np.full((h, w), -1, dtype=np.int32)
        S   = self.segments
        N   = self.leds_per_segment
        gap = self.gap_px
        if S == 0 or N == 0 or w < 4 or h < 4:
            return m

        px_x, px_y = 10, 6
        uw   = max(1, w - 2 * px_x)
        uh   = max(1, h - 2 * px_y)
        # leave gap above first strip, between all strips, and below last
        lh   = max(2, (uh - gap * (S + 1)) // S)

        # Use linspace so all N LEDs share the full usable width evenly
        bounds = np.linspace(0, uw, N + 1).astype(np.int32)

        for seg in range(S):
            y0 = px_y + gap + seg * (lh + gap)
            y1 = min(h, y0 + lh)
            if y0 >= h:
                break
            for led in range(N):
                idx = seg * N + led
                if seg % 2 == 0:          # left → right
                    x0 = px_x + int(bounds[led])
                    x1 = px_x + int(bounds[led + 1])
                else:                     # right → left (snake)
                    x0 = px_x + uw - int(bounds[led + 1])
                    x1 = px_x + uw - int(bounds[led])
                x0 = max(0, x0)
                x1 = min(w, x1)
                if x1 <= x0:
                    continue
                m[y0:y1, x0:x1] = idx
        return m


# ── Globe ─────────────────────────────────────────────────────────────────────

class GlobeGeometry(Geometry):
    """3-D LED ball (glorb) mapped onto a 2-D canvas.

    All n LEDs are placed on a sphere surface using the Fibonacci (golden-angle)
    method, then orthographically projected to a disc.  Every canvas pixel inside
    the disc is assigned to its nearest LED via a Voronoi map, so all n LEDs are
    always visible regardless of whether they are on the front or back hemisphere.

    wiring options (how LED index advances around the sphere):
      sphere  — Fibonacci sphere, uniform 3-D distribution (default)
      rings   — latitude rings, LED 0 at top, snaking left/right
      spiral  — single longitude spiral top-to-bottom
      grid    — explicit W×H matrix (e.g. WLED 2D setup). Requires grid_w/grid_h.
                Row 0 = top ring, row H-1 = bottom ring. Non-serpentine left→right.
    """
    type_name = "globe"

    _GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))   # ≈ 137.508°

    def __init__(self, led_count: int = 120, rotation_offset: int = 0,
                 mapping: str = "sphere", sort_axis: str = "y",
                 grid_w: int = 0, grid_h: int = 0):
        self._n              = int(led_count)
        self.rotation_offset = int(rotation_offset)
        self.mapping         = mapping
        self.sort_axis       = sort_axis   # "y"=top-bottom  "x"=left-right  "z"=front-back
        self.grid_w          = int(grid_w)
        self.grid_h          = int(grid_h)
        self._led_order      = None        # cached sort permutation

    def n_leds(self):
        return self._n

    def to_dict(self):
        d = {"type": "globe",
             "led_count": self._n,
             "rotation_offset": self.rotation_offset,
             "mapping": self.mapping,
             "sort_axis": self.sort_axis}
        if self.mapping == "grid":
            d["grid_w"] = self.grid_w
            d["grid_h"] = self.grid_h
        return d

    def _grid_points(self) -> np.ndarray:
        """Return (n, 3) float32 sphere coordinates for a W×H grid wiring.
        Row 0 = top ring (latitude +90°), row H-1 = bottom ring (latitude -90°).
        Within each row LEDs go left→right (longitude 0→360°, non-serpentine).
        """
        w, h = self.grid_w, self.grid_h
        if w <= 0 or h <= 0 or w * h != self._n:
            # Fallback: treat as single row
            w, h = self._n, 1
        pts = np.zeros((self._n, 3), dtype=np.float32)
        rot = math.radians(self.rotation_offset)
        for i in range(self._n):
            row = i // w
            col = i % w
            lat = math.pi / 2 - (row + 0.5) / h * math.pi   # +π/2 (top) → -π/2 (bottom)
            lon = col / w * 2 * math.pi + rot
            r   = math.cos(lat)
            pts[i] = (math.cos(lon) * r, math.sin(lat), math.sin(lon) * r)
        return pts

    def ring_render_size(self):
        """For grid mapping: return (ring_count, ring_size) so effects render at
        ring_count resolution and each pixel gets tiled across ring_size LEDs.
        Returns None for non-grid mappings."""
        if self.mapping == "grid" and self.grid_w > 0 and self.grid_h > 0:
            return (self.grid_h, self.grid_w)
        return None

    def led_order(self) -> np.ndarray:
        """Return permutation array: led_order[i] = physical LED index for effect position i.
        Cached — computed once on first call."""
        if self._led_order is not None:
            return self._led_order
        if self.mapping == "grid":
            pts = self._grid_points()
        else:
            pts = self._sphere_points(self._n, math.radians(self.rotation_offset))
        axis_map = {"x": 0, "y": 1, "z": 2}
        col = axis_map.get(self.sort_axis, 1)
        # Sort descending so position 0 = top/front/right
        self._led_order = np.argsort(-pts[:, col]).astype(np.int32)
        return self._led_order

    # ── 3-D point generation ──────────────────────────────────────────────────

    def _sphere_points(self, n: int, rot: float) -> np.ndarray:
        """Return (n, 3) float32 array of unit-sphere points."""
        pts = np.zeros((n, 3), dtype=np.float32)
        if self.mapping == "sphere":
            # Fibonacci / golden-angle: most uniform possible distribution
            for i in range(n):
                y        = 1.0 - (2.0 * i + 1.0) / n   # top → bottom
                r        = math.sqrt(max(0.0, 1.0 - y * y))
                theta    = self._GOLDEN_ANGLE * i + rot
                pts[i]   = (math.cos(theta) * r, y, math.sin(theta) * r)
        elif self.mapping == "rings":
            # Horizontal rings snaking top→bottom, counts proportional to circumference
            n_rings = max(2, round(math.sqrt(n * math.pi / 4)))
            lats    = [-math.pi / 2 + (j + 0.5) / n_rings * math.pi
                       for j in range(n_rings)]
            circs   = [max(0.01, math.cos(la)) for la in lats]
            total_c = sum(circs)
            counts  = []
            rem     = n
            for c in circs[:-1]:
                cnt = max(1, round(n * c / total_c))
                counts.append(cnt); rem -= cnt
            counts.append(max(1, rem))
            idx = 0
            for j, (lat, count) in enumerate(zip(lats, counts)):
                r = math.cos(lat); y = math.sin(lat)
                for s in range(count):
                    if idx >= n: break
                    s_adj = s if j % 2 == 0 else (count - 1 - s)
                    lon   = s_adj / count * 2 * math.pi + rot
                    pts[idx] = (math.cos(lon) * r, y, math.sin(lon) * r)
                    idx += 1
            for i in range(idx, n):    # fallback for any rounding remainder
                y2     = 1.0 - (2.0 * i + 1.0) / n
                r2     = math.sqrt(max(0.0, 1.0 - y2 * y2))
                theta  = self._GOLDEN_ANGLE * i + rot
                pts[i] = (math.cos(theta) * r2, y2, math.sin(theta) * r2)
        else:  # spiral — single helix top-to-bottom
            for i in range(n):
                y        = 1.0 - (2.0 * i) / max(1, n - 1)
                r        = math.sqrt(max(0.0, 1.0 - y * y))
                theta    = i / max(1, n - 1) * 6 * math.pi + rot   # 3 full turns
                pts[i]   = (math.cos(theta) * r, y, math.sin(theta) * r)
        return pts

    # ── LED map ───────────────────────────────────────────────────────────────

    def build_led_map(self, w, h):
        m = np.full((h, w), -1, dtype=np.int32)
        n = self._n
        if n == 0 or w < 4 or h < 4:
            return m

        rot   = math.radians(self.rotation_offset)
        pts3d = (self._grid_points() if self.mapping == "grid"
                 else self._sphere_points(n, rot))   # (n, 3)  unit sphere

        if self.mapping == "sphere":
            # ── Orthographic disc (Fibonacci sphere always has unique 2-D projections)
            cx, cy = w / 2.0, h / 2.0
            max_r  = min(w, h) / 2.0 - 8
            led_2d = np.empty((n, 2), dtype=np.float32)
            led_2d[:, 0] =  pts3d[:, 0] * max_r + cx
            led_2d[:, 1] = -pts3d[:, 1] * max_r + cy
            ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
            inside = ((xs - cx) ** 2 + (ys - cy) ** 2) <= max_r ** 2
        else:
            # ── Equirectangular rectangle (avoids orthographic symmetry collisions)
            # lon ∈ (-π, π) → x ∈ (pad, w-pad)
            # lat ∈ (-π/2, π/2) → y ∈ (pad, h-pad)
            pad = 8
            lon = np.arctan2(pts3d[:, 2], pts3d[:, 0])          # (-π, π)
            lat = np.arcsin(np.clip(pts3d[:, 1], -1.0, 1.0))    # (-π/2, π/2)
            led_2d = np.empty((n, 2), dtype=np.float32)
            led_2d[:, 0] = (lon + math.pi) / (2 * math.pi) * (w - 2 * pad) + pad
            led_2d[:, 1] = (math.pi / 2 - lat) / math.pi * (h - 2 * pad) + pad
            ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
            inside = np.ones((h, w), dtype=bool)                 # full rectangle

        flat_x = xs[inside]
        flat_y = ys[inside]
        pts_2d = np.stack([flat_x, flat_y], axis=1)              # (M, 2)

        # Voronoi nearest-LED assignment (chunked to limit peak memory)
        nearest = np.empty(len(pts_2d), dtype=np.int32)
        chunk   = 4096
        for start in range(0, len(pts_2d), chunk):
            end   = min(start + chunk, len(pts_2d))
            diff  = pts_2d[start:end, np.newaxis, :] - led_2d[np.newaxis, :, :]
            dist2 = (diff * diff).sum(axis=-1)
            nearest[start:end] = dist2.argmin(axis=-1)

        m[inside] = nearest
        return m


# ── Rainbow preview helper (for editor dialog) ────────────────────────────────

def rainbow_preview(led_map: np.ndarray) -> np.ndarray:
    """Render led_map with HSV rainbow colours (LED 0 = red, last = red).
    Returns (H × W × 3) uint8 array."""
    h, w   = led_map.shape
    img    = np.full((h, w, 3), 10, dtype=np.uint8)
    valid  = led_map >= 0
    if not valid.any():
        return img
    n_leds  = int(led_map.max()) + 1
    indices = led_map[valid].astype(float)
    hues    = indices / max(1, n_leds)
    # HSV → RGB (saturation=1, value=1)
    h6  = hues * 6.0
    hi  = h6.astype(int) % 6
    f   = h6 - np.floor(h6)
    q   = 1.0 - f
    rgb = np.zeros((len(hues), 3))
    for sector, (rv, gv, bv) in enumerate([
        (1, f, 0), (q, 1, 0), (0, 1, f),
        (0, q, 1), (f, 0, 1), (1, 0, q),
    ]):
        mask = hi == sector
        rgb[mask, 0] = np.array(rv)[mask] if hasattr(rv, '__len__') else rv
        rgb[mask, 1] = np.array(gv)[mask] if hasattr(gv, '__len__') else gv
        rgb[mask, 2] = np.array(bv)[mask] if hasattr(bv, '__len__') else bv
    img[valid] = (rgb * 255).clip(0, 255).astype(np.uint8)
    return img
