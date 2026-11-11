"""ADVANCED effects — sentinel stub classes for the DDPCanvas persistent-buffer engine.

These appear as buttons in the ADVANCED category of the EffectsPanel.
When selected, the main render loop routes output through AdvancedRenderer
instead of the standard procedural pipeline.

render() returns zeros — it is never called for strips in advanced mode.
"""
import numpy as np
from .base import BaseEffect


def _make(pattern_key: str, display_name: str):
    return type(
        f"Adv_{pattern_key}",
        (BaseEffect,),
        {
            "name":             display_name,
            "category":         "advanced",
            "advanced_pattern": pattern_key,
            "render": lambda self, t, bpm, n, palette, speed: np.zeros((n, 3), dtype=np.uint8),
        },
    )


# ── Meteor-family ──────────────────────────────────────────────────────────
MeteorStorm  = _make("meteor_storm",  "METEOR\nSTORM")
DualMeteor   = _make("dual_meteor",   "DUAL\nMETEOR")
CometPulse   = _make("comet_pulse",   "COMET\nPULSE")
BeatLaunch   = _make("beat_launch",   "BEAT\nLAUNCH")
ChaosSwarm   = _make("chaos_swarm",   "CHAOS\nSWARM")

# ── Ambient / atmospheric ─────────────────────────────────────────────────
FireflySwarm = _make("firefly_swarm", "FIREFLY\nSWARM")
DeepSpace    = _make("deep_space",    "DEEP\nSPACE")
SparkleField = _make("sparkle_field", "SPARKLE\nFIELD")

# ── Physics / particle ────────────────────────────────────────────────────
ExplosionBurst = _make("explosion_burst", "EXPLO\nBURST")
FireworkShow   = _make("firework_show",   "FIRE\nWORKS")
GlitterBurst   = _make("glitter_burst",   "GLITTER\nBURST")

# ── Shockwave / pulse ─────────────────────────────────────────────────────
PulseExpand    = _make("pulse_expand",    "PULSE\nEXPAND")
ShockwavePulse = _make("shockwave_pulse", "SHOCK\nWAVE")


EFFECTS = [
    MeteorStorm, DualMeteor, CometPulse, BeatLaunch, ChaosSwarm,
    FireflySwarm, DeepSpace, SparkleField,
    ExplosionBurst, FireworkShow, GlitterBurst,
    PulseExpand, ShockwavePulse,
]
