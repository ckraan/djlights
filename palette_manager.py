"""Palette management — load, save, interpolate."""
import numpy as np
from config_manager import load_palettes, save_palettes


def _interp(stops, pos):
    """Interpolate sorted stops at pos 0-1. Returns (r, g, b) ints."""
    if not stops:
        return (255, 255, 255)
    if pos <= stops[0]["pos"]:
        s = stops[0]; return (s["r"], s["g"], s["b"])
    if pos >= stops[-1]["pos"]:
        s = stops[-1]; return (s["r"], s["g"], s["b"])
    for i in range(len(stops) - 1):
        a, b = stops[i], stops[i + 1]
        if a["pos"] <= pos <= b["pos"]:
            t = (pos - a["pos"]) / max(1e-9, b["pos"] - a["pos"])
            return (int(a["r"] + t * (b["r"] - a["r"])),
                    int(a["g"] + t * (b["g"] - a["g"])),
                    int(a["b"] + t * (b["b"] - a["b"])))
    s = stops[-1]; return (s["r"], s["g"], s["b"])


class Palette:
    def __init__(self, data: dict):
        self.name  = data["name"]
        self.stops = sorted(data.get("stops", []), key=lambda s: s["pos"])

    def color(self, pos: float):
        """Return (r, g, b) at position pos (0-1)."""
        return _interp(self.stops, float(np.clip(pos, 0.0, 1.0)))

    def to_array(self, n=256) -> np.ndarray:
        """Build a (n,3) uint8 LUT for fast indexing."""
        lut = np.zeros((n, 3), dtype=np.uint8)
        for i in range(n):
            r, g, b = self.color(i / max(1, n - 1))
            lut[i] = (r, g, b)
        return lut

    def to_dict(self):
        return {"name": self.name, "stops": self.stops}


class PaletteManager:
    def __init__(self):
        self.palettes = [Palette(d) for d in load_palettes()]
        self._active  = 0

    @property
    def active(self) -> Palette:
        if not self.palettes:
            return Palette({"name": "White", "stops": [
                {"pos": 0.0, "r": 255, "g": 255, "b": 255},
                {"pos": 1.0, "r": 255, "g": 255, "b": 255}]})
        return self.palettes[self._active]

    def set_active(self, idx: int):
        self._active = max(0, min(idx, len(self.palettes) - 1))

    def save(self):
        save_palettes([p.to_dict() for p in self.palettes])

    def add(self, name: str, stops: list):
        self.palettes.append(Palette({"name": name, "stops": stops}))
        self.save()

    def remove(self, idx: int):
        if 0 <= idx < len(self.palettes):
            self.palettes.pop(idx)
            self._active = max(0, min(self._active, len(self.palettes) - 1))
            self.save()

    def rename(self, idx: int, name: str):
        if 0 <= idx < len(self.palettes):
            self.palettes[idx].name = name
            self.save()

    def get_by_name(self, palette_name):
        """Find palette by name string (case-insensitive). Returns index or None."""
        if not palette_name:
            return None
        name_lower = palette_name.lower()
        for idx, palette in enumerate(self.palettes):
            if palette.name.lower() == name_lower:
                return idx
        return None

    def get_available_for_phrase(self, phrase_type):
        """Return palette names for this phrase type, filtered to only loaded palettes."""
        if not phrase_type:
            return []
        from wled_ctrl import PHRASE_PALETTE_POOLS
        pool = PHRASE_PALETTE_POOLS.get(phrase_type.lower(), [])
        loaded = {p.name for p in self.palettes}
        return [name for name in pool if name in loaded]
