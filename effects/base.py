"""Base effect class."""
import numpy as np
from typing import Callable, Tuple

PaletteFunc = Callable[[float], Tuple[int, int, int]]


def _gauss(positions: np.ndarray, center: float, sigma: float) -> np.ndarray:
    """Gaussian envelope, NOT wrap-around."""
    return np.exp(-((positions - center) ** 2) / max(1e-9, 2 * sigma ** 2))


def _gauss_wrap(positions: np.ndarray, center: float, sigma: float, n: int) -> np.ndarray:
    """Gaussian envelope with wrap-around distance."""
    dist = np.minimum((positions - center) % n, (center - positions) % n)
    return np.exp(-(dist ** 2) / max(1e-9, 2 * sigma ** 2))


def apply_color(px: np.ndarray, indices: np.ndarray,
                mask: np.ndarray, r: int, g: int, b: int):
    """Write colour × mask into px at positions indices (clamp existing)."""
    m = np.clip(mask, 0.0, 1.0)
    px[indices, 0] = np.clip(px[indices, 0] + r * m, 0, 255).astype(np.uint8)
    px[indices, 1] = np.clip(px[indices, 1] + g * m, 0, 255).astype(np.uint8)
    px[indices, 2] = np.clip(px[indices, 2] + b * m, 0, 255).astype(np.uint8)


def _palette_array(palette_fn, positions: np.ndarray) -> np.ndarray:
    """Sample palette_fn at an array of positions (0-1). Returns (n, 3) float32.
    Builds a 256-step LUT (256 Python calls) then indexes with numpy — much faster
    than calling palette_fn once per LED (n=1500 calls)."""
    lut = np.zeros((256, 3), dtype=np.float32)
    for i in range(256):
        r, g, b = palette_fn(i / 255.0)
        lut[i] = (r, g, b)
    idx = np.clip((np.asarray(positions, dtype=np.float32) * 255).astype(np.int32), 0, 255)
    return lut[idx]


def _tail_mask(arr: np.ndarray, pos: float, tail_len: float, power: float = 2.0) -> np.ndarray:
    """Vectorized comet tail: pixels behind pos by up to tail_len get a fade."""
    d = pos - arr
    m = np.zeros(len(arr), dtype=np.float32)
    mask = (d > 0) & (d < tail_len)
    m[mask] = ((tail_len - d[mask]) / tail_len) ** power
    return m


class BaseEffect:
    name: str = "Effect"
    category: str = ""

    def render(self, t: float, bpm: float, n: int,
               palette: PaletteFunc, speed: float) -> np.ndarray:
        """Return (n, 3) uint8 pixel array. Override in subclasses."""
        return np.zeros((n, 3), dtype=np.uint8)
