"""Stub effect classes that represent AnimationEngine render modes.

These are NOT rendered directly — clicking them sets choreo_mgr._mode
so the ChoreographyManager uses that AnimationEngine fill function.
Each class has a `choreo_mode` attribute with the internal mode string.
"""
import numpy as np
from .base import BaseEffect


def _mode(internal_name, label):
    cls = type(label.replace("\n", "_").replace(" ", "_"), (BaseEffect,), {
        "name":       label,
        "category":   "choreo",
        "choreo_mode": internal_name,
        "render":     lambda self, t, bpm, n, palette, speed: np.zeros((n, 3), dtype=np.uint8),
    })
    return cls


DiamondWave      = _mode("diamond_wave",      "DIAMOND\nWAVE")
DiamondChase     = _mode("diamond_chase",     "DIAMOND\nCHASE")
DiamondBloom     = _mode("diamond_bloom",     "DIAMOND\nBLOOM")
MinimalAmbient   = _mode("minimal_ambient",   "MINIMAL\nAMBIENT")
EmberDriftMode   = _mode("ember_drift",       "EMBER\nDRIFT")
DeepVoid         = _mode("deep_void",         "DEEP\nVOID")
DigitalRain      = _mode("digital_rain",      "DIGITAL\nRAIN")
MirrorSymmetry   = _mode("mirror_symmetry",   "MIRROR\nSYM")
VerticalPulse    = _mode("vertical_pulse",    "VERT\nPULSE")
NoiseField       = _mode("noise",             "NOISE\nFIELD")

EFFECTS = [
    DiamondWave, DiamondChase, DiamondBloom,
    MinimalAmbient, EmberDriftMode, DeepVoid,
    DigitalRain, MirrorSymmetry, VerticalPulse, NoiseField,
]
