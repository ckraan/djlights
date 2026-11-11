"""VAULT pattern stubs — sentinel classes that route rendering to VaultRenderer.

These appear as buttons in the VAULT category of the EffectsPanel.
When selected, the main render loop uses VaultRenderer instead of the
standard effect pipeline for non-globe (snake-wired) devices.

The render() method is a no-op fallback; it is never called for vault devices.
"""
import numpy as np
from .base import BaseEffect


def _make(pattern_key: str, display_name: str):
    """Factory: creates a VaultPatternEffect subclass for the given pattern."""
    return type(
        f"Vault_{pattern_key}",
        (BaseEffect,),
        {
            "name":          display_name,
            "category":      "vault",
            "vault_pattern": pattern_key,
            "render": lambda self, t, bpm, n, palette, speed: np.zeros((n, 3), dtype=np.uint8),
        }
    )


MatrixRain    = _make("matrix_rain",    "MATRIX\nRAIN")
Sparks        = _make("sparks",         "SPARKS")
PingPong      = _make("ping_pong",      "PING\nPONG")
Warp          = _make("warp",           "WARP")
MeteorShower  = _make("meteor_shower",  "METEOR\nSHOWER")
Fireflies     = _make("fireflies",      "FIRE\nFLIES")
DnaHelix      = _make("dna_helix",      "DNA\nHELIX")
Raindrops     = _make("raindrops",      "RAIN\nDROPS")
Orbit         = _make("orbit",          "ORBIT")
DiagCross     = _make("diagonal_cross", "DIAG\nCROSS")
Zigzag        = _make("zigzag",         "ZIGZAG")
Spotlight     = _make("spotlight",      "SPOT\nLIGHT")
CornerBurst   = _make("corner_burst",   "CORNER\nBURST")
RadialRings   = _make("radial_rings",   "RADIAL\nRINGS")
Chevron       = _make("chevron",        "CHEVRON")
Plasma        = _make("plasma",         "PLASMA")

EFFECTS = [
    MatrixRain, Sparks, PingPong, Warp, MeteorShower, Fireflies,
    DnaHelix, Raindrops, Orbit, DiagCross, Zigzag, Spotlight,
    CornerBurst, RadialRings, Chevron, Plasma,
]
