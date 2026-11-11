"""
test_ai_extended.py — Extended LightAI test: full DJ set simulation with loops.

Usage:
    python test_ai_extended.py                        # uses ANTHROPIC_API_KEY env var
    python test_ai_extended.py --key sk-ant-...
    python test_ai_extended.py --group full_set
    python test_ai_extended.py --group loops
    python test_ai_extended.py --group transitions
    python test_ai_extended.py --group energy
    python test_ai_extended.py --group all
"""
import argparse
import os
import sys
import time

SYSTEM_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "light_ai.md")

# ── Context builder ────────────────────────────────────────────────────────────

PHRASE_EFFECTS = {
    "intro":  ["NightSky", "FogDrift", "TwilightWave", "DualBreath", "PulseRipple", "GlowCenter"],
    "verse":  ["LaserScan", "PulseDuo", "MeteorShower", "ScanLine", "BeatFlash", "TripleChase"],
    "bridge": ["CrossfadeSpots", "RotatingBeam", "ColdBreath", "SingleBreath", "LongBreath", "NightSky"],
    "chorus": ["StrobeStorm", "DualLaserChase", "ImpactWaves", "ExplosionBurst", "RaveStrobe", "FlashBeat"],
    "up":     ["LaserScan", "TripleChase", "BeatFlash", "ImpactWaves", "MeteorShower", "PulseDuo"],
    "down":   ["SingleBreath", "LongBreath", "SparseDrift", "EchoPulse", "MemoryFade", "CrossfadeSpots"],
    "outro":  ["NightSky", "FogDrift", "GlowCenter", "MemoryFade", "SparseDrift", "ColdBreath"],
}
PHRASE_PALETTES = {
    "intro":  ["Ocean", "Sunset", "Ice", "BlueOnly"],
    "verse":  ["bro", "Sunset", "DeepHouse"],
    "bridge": ["Ocean", "BlueOnly", "Ice"],
    "chorus": ["Neon", "Cyberpunk", "Fire", "beam", "Contrast"],
    "up":     ["bro", "Sunset", "Neon", "Cyberpunk"],
    "down":   ["Ocean", "Sunset"],
    "outro":  ["Sunset", "Ocean", "Monochrome"],
}


def _ctx(phrase_type, beat_in_phrase=1, total_beats=32, beats_to_next=28,
         next_phrase=None, bpm=128.0, beat_within_bar=1, is_looping=False,
         is_master=True, current_effect=None, current_palette="Ocean",
         time_in_effect=2.0, event=None, loop_length=None,
         current_speed=1.0, current_direction="forward", current_grouping=1,
         current_spacing=0, current_offset=0.0, current_density=1.0,
         available_effects=None, available_palettes=None):
    return {
        "phrase_type":            phrase_type,
        "beat_in_phrase":         beat_in_phrase,
        "total_beats_in_phrase":  total_beats,
        "beats_to_next_phrase":   beats_to_next,
        "next_phrase_type":       next_phrase,
        "bpm":                    bpm,
        "beat_within_bar":        beat_within_bar,
        "is_looping":             is_looping,
        "is_master":              is_master,
        "current_effect":         current_effect,
        "current_palette":        current_palette,
        "time_in_current_effect": time_in_effect,
        "event":                  event,
        "loop_length":            loop_length,
        "current_speed":          current_speed,
        "current_direction":      current_direction,
        "current_grouping":       current_grouping,
        "current_spacing":        current_spacing,
        "current_offset":         current_offset,
        "current_density":        current_density,
        "available_effects":      available_effects or PHRASE_EFFECTS.get(phrase_type, []),
        "available_palettes":     available_palettes or PHRASE_PALETTES.get(phrase_type, []),
    }


# ── Scenario groups ────────────────────────────────────────────────────────────

# ── GROUP 1: Full set — Track 1 (132 BPM Techno) ─────────────────────────────
FULL_SET = [
    {
        "name": "[T1] Intro — track starts, beat 1",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean"),
    },
    {
        "name": "[T1] Intro — mid-phrase, effect established (beat 12)",
        "context": _ctx("intro", beat_in_phrase=12, total_beats=32, beats_to_next=20,
                        next_phrase="verse", bpm=132.0,
                        current_effect="NightSky", current_palette="Ocean", time_in_effect=5.5),
    },
    {
        "name": "[T1] Intro → verse approaching (5 beats out)",
        "context": _ctx("intro", beat_in_phrase=27, total_beats=32, beats_to_next=5,
                        next_phrase="verse", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="TwilightWave", current_palette="Ice", time_in_effect=11.4),
    },
    {
        "name": "[T1] Verse — drop, beat 1",
        "context": _ctx("verse", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="TwilightWave", current_palette="Ice", time_in_effect=0.5),
    },
    {
        "name": "[T1] Verse — mid-phrase (beat 10, effect maturing)",
        "context": _ctx("verse", beat_in_phrase=10, total_beats=32, beats_to_next=22,
                        next_phrase="chorus", bpm=132.0,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=4.5),
    },
    {
        "name": "[T1] Verse — 4-bar reroll (beat 17, effect stale at 13s)",
        "context": _ctx("verse", beat_in_phrase=17, total_beats=32, beats_to_next=15,
                        next_phrase="chorus", bpm=132.0,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=13.6),
    },
    {
        "name": "[T1] Verse → chorus — 4 beats out (anticipation)",
        "context": _ctx("verse", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase="chorus", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="Sunset", time_in_effect=8.7),
    },
    {
        "name": "[T1] Chorus — first drop, beat 1",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="Sunset", time_in_effect=0.5),
    },
    {
        "name": "[T1] Chorus — peak energy (beat 8)",
        "context": _ctx("chorus", beat_in_phrase=8, total_beats=32, beats_to_next=24,
                        next_phrase="bridge", bpm=132.0,
                        current_effect="StrobeStorm", current_palette="Neon", time_in_effect=3.6),
    },
    {
        "name": "[T1] Chorus — 4-bar reroll (beat 17, effect stale at 15s)",
        "context": _ctx("chorus", beat_in_phrase=17, total_beats=32, beats_to_next=15,
                        next_phrase="bridge", bpm=132.0,
                        current_effect="RaveStrobe", current_palette="Cyberpunk", time_in_effect=15.2),
    },
    {
        "name": "[T1] Chorus → bridge — 4 beats out",
        "context": _ctx("chorus", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase="bridge", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="FlashBeat", current_palette="Fire", time_in_effect=7.9),
    },
    {
        "name": "[T1] Bridge — atmospheric entry, beat 1",
        "context": _ctx("bridge", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="FlashBeat", current_palette="Fire", time_in_effect=0.5),
    },
    {
        "name": "[T1] Bridge — mid-phrase (beat 16)",
        "context": _ctx("bridge", beat_in_phrase=16, total_beats=32, beats_to_next=16,
                        next_phrase="chorus", bpm=132.0,
                        current_effect="ColdBreath", current_palette="Ocean", time_in_effect=7.3),
    },
    {
        "name": "[T1] Bridge → chorus (second drop) — 3 beats out",
        "context": _ctx("bridge", beat_in_phrase=29, total_beats=32, beats_to_next=3,
                        next_phrase="chorus", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="RotatingBeam", current_palette="BlueOnly", time_in_effect=14.9),
    },
    {
        "name": "[T1] Chorus — second drop, beat 1",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="outro", bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="RotatingBeam", current_palette="BlueOnly", time_in_effect=0.5),
    },
    {
        "name": "[T1] Outro — graceful wind-down, beat 1",
        "context": _ctx("outro", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase=None, bpm=132.0,
                        event="phrase_change_incoming",
                        current_effect="ImpactWaves", current_palette="Neon", time_in_effect=0.5),
    },
    {
        "name": "[T1] Outro — fading out (beat 20)",
        "context": _ctx("outro", beat_in_phrase=20, total_beats=32, beats_to_next=12,
                        next_phrase=None, bpm=132.0,
                        current_effect="FogDrift", current_palette="Sunset", time_in_effect=9.1),
    },

    # ── Track 2: House at 124 BPM ─────────────────────────────────────────────
    {
        "name": "[T2] New track — intro at 124 BPM",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=16, beats_to_next=15,
                        next_phrase="up", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean"),
    },
    {
        "name": "[T2] Up-phrase — building energy (beat 8 of 32)",
        "context": _ctx("up", beat_in_phrase=8, total_beats=32, beats_to_next=24,
                        next_phrase="chorus", bpm=124.0,
                        current_effect="RisingComets", current_palette="bro", time_in_effect=3.9),
    },
    {
        "name": "[T2] Up → chorus — 4 beats out (final build)",
        "context": _ctx("up", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase="chorus", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect="BeatFlash", current_palette="Neon", time_in_effect=15.5),
    },
    {
        "name": "[T2] Chorus — house drop, beat 1",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="down", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect="BeatFlash", current_palette="Neon", time_in_effect=0.5),
    },
    {
        "name": "[T2] Down-phrase — post-drop breakdown, beat 1",
        "context": _ctx("down", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect="ExplosionBurst", current_palette="Fire", time_in_effect=0.5),
    },
    {
        "name": "[T2] Down — deep in breakdown (beat 20, de-escalating)",
        "context": _ctx("down", beat_in_phrase=20, total_beats=32, beats_to_next=12,
                        next_phrase="verse", bpm=124.0,
                        current_effect="MemoryFade", current_palette="Ocean", time_in_effect=9.7),
    },
]

# ── GROUP 2: Loop scenarios ───────────────────────────────────────────────────
LOOPS = [
    {
        "name": "[LOOP] 2-beat tight loop — just activated (verse)",
        "context": _ctx("verse", beat_in_phrase=6, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=2,
                        bpm=128.0,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=2.8),
    },
    {
        "name": "[LOOP] 2-beat loop — second cycle (should stay, too short to change)",
        "context": _ctx("verse", beat_in_phrase=8, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=2,
                        bpm=128.0,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=4.5),
    },
    {
        "name": "[LOOP] 4-beat loop — activated (chorus energy)",
        "context": _ctx("chorus", beat_in_phrase=9, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=4,
                        bpm=132.0,
                        current_effect="FlashBeat", current_palette="Neon", time_in_effect=4.1),
    },
    {
        "name": "[LOOP] 4-beat loop — still held (12s in, should stay tight)",
        "context": _ctx("chorus", beat_in_phrase=9, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=4,
                        bpm=132.0,
                        current_effect="FlashBeat", current_palette="Neon", time_in_effect=12.1),
    },
    {
        "name": "[LOOP] 4-beat loop — verse energy (DJ slowing things down)",
        "context": _ctx("verse", beat_in_phrase=14, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=4,
                        bpm=126.0,
                        current_effect="ScanLine", current_palette="Sunset", time_in_effect=6.0),
    },
    {
        "name": "[LOOP] 8-beat loop — activated (chorus, room for variety)",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        bpm=128.0,
                        current_effect="RaveStrobe", current_palette="Cyberpunk", time_in_effect=0.5),
    },
    {
        "name": "[LOOP] 8-beat loop — first refresh (beat 8)",
        "context": _ctx("chorus", beat_in_phrase=8, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        bpm=128.0,
                        current_effect="RaveStrobe", current_palette="Cyberpunk", time_in_effect=7.5),
    },
    {
        "name": "[LOOP] 8-beat loop — second refresh (beat 16)",
        "context": _ctx("chorus", beat_in_phrase=16, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        bpm=128.0,
                        current_effect="StrobeStorm", current_palette="Fire", time_in_effect=8.1),
    },
    {
        "name": "[LOOP] 8-beat loop — third refresh (beat 24, keep energy high)",
        "context": _ctx("chorus", beat_in_phrase=24, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        bpm=128.0,
                        current_effect="ImpactWaves", current_palette="Neon", time_in_effect=8.0),
    },
    {
        "name": "[LOOP] 16-beat loop — extended breakdown (bridge energy)",
        "context": _ctx("bridge", beat_in_phrase=1, total_beats=64, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=16,
                        bpm=124.0,
                        current_effect="StrobeStorm", current_palette="Fire", time_in_effect=0.5),
    },
    {
        "name": "[LOOP] 16-beat loop — mid-way refresh (beat 16)",
        "context": _ctx("bridge", beat_in_phrase=16, total_beats=64, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=16,
                        bpm=124.0,
                        current_effect="ColdBreath", current_palette="Ocean", time_in_effect=15.5),
    },
    {
        "name": "[LOOP] Unknown loop length — just detected, no length yet",
        "context": _ctx("verse", beat_in_phrase=4, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length="unknown",
                        bpm=130.0,
                        current_effect="BeatFlash", current_palette="bro", time_in_effect=3.1),
    },
    {
        "name": "[LOOP] Loop in up-phrase — should build with loop energy",
        "context": _ctx("up", beat_in_phrase=16, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        bpm=132.0,
                        current_effect="TripleChase", current_palette="Neon", time_in_effect=7.9),
    },
    {
        "name": "[LOOP] Loop in down-phrase — de-escalating, keep it subtle",
        "context": _ctx("down", beat_in_phrase=8, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=4,
                        bpm=126.0,
                        current_effect="EchoPulse", current_palette="Ocean", time_in_effect=6.1),
    },
]

# ── GROUP 3: Phrase transitions ───────────────────────────────────────────────
TRANSITIONS = [
    {
        "name": "[TRANS] Verse → chorus — 6 beats out (early warning)",
        "context": _ctx("verse", beat_in_phrase=26, total_beats=32, beats_to_next=6,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="MeteorShower", current_palette="bro", time_in_effect=12.5),
    },
    {
        "name": "[TRANS] Verse → chorus — 2 beats out (very imminent)",
        "context": _ctx("verse", beat_in_phrase=30, total_beats=32, beats_to_next=2,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="Sunset", time_in_effect=14.1),
    },
    {
        "name": "[TRANS] Chorus → bridge — 6 beats out (cool-down coming)",
        "context": _ctx("chorus", beat_in_phrase=26, total_beats=32, beats_to_next=6,
                        next_phrase="bridge", bpm=130.0,
                        event="phrase_change_incoming",
                        current_effect="DualLaserChase", current_palette="Cyberpunk", time_in_effect=11.8),
    },
    {
        "name": "[TRANS] Bridge → chorus — 6 beats out (second drop incoming)",
        "context": _ctx("bridge", beat_in_phrase=26, total_beats=32, beats_to_next=6,
                        next_phrase="chorus", bpm=130.0,
                        event="phrase_change_incoming",
                        current_effect="SingleBreath", current_palette="BlueOnly", time_in_effect=12.3),
    },
    {
        "name": "[TRANS] Down → verse — energy recovering (6 beats out)",
        "context": _ctx("down", beat_in_phrase=26, total_beats=32, beats_to_next=6,
                        next_phrase="verse", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect="SparseDrift", current_palette="Ocean", time_in_effect=12.0),
    },
    {
        "name": "[TRANS] Outro — final bars, next phrase is nothing (track ending)",
        "context": _ctx("outro", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase=None, bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="FogDrift", current_palette="Sunset", time_in_effect=13.5),
    },
    {
        "name": "[TRANS] Player not master — effect should be subtle",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", bpm=128.0, is_master=False,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean"),
    },
    {
        "name": "[TRANS] Cue jump — beat jumped, now at start of verse (beat 1)",
        "context": _ctx("verse", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="StrobeStorm", current_palette="Fire", time_in_effect=0.2),
    },
]

# ── GROUP 4: Energy and BPM variety ──────────────────────────────────────────
ENERGY = [
    {
        "name": "[ENERGY] Slow groove at 90 BPM — intro",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", bpm=90.0,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean"),
    },
    {
        "name": "[ENERGY] Slow groove at 90 BPM — verse",
        "context": _ctx("verse", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=90.0,
                        event="phrase_change_incoming",
                        current_effect="NightSky", current_palette="Sunset", time_in_effect=0.5),
    },
    {
        "name": "[ENERGY] Hard techno at 145 BPM — chorus",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", bpm=145.0,
                        event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="bro", time_in_effect=0.5),
    },
    {
        "name": "[ENERGY] Hard techno at 145 BPM — chorus reroll (beat 17)",
        "context": _ctx("chorus", beat_in_phrase=17, total_beats=32, beats_to_next=15,
                        next_phrase="bridge", bpm=145.0,
                        current_effect="RaveStrobe", current_palette="Neon", time_in_effect=14.0),
    },
    {
        "name": "[ENERGY] Extended chorus (64 beats) — beat 1",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=64, beats_to_next=63,
                        next_phrase="bridge", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="BeatFlash", current_palette="bro", time_in_effect=0.5),
    },
    {
        "name": "[ENERGY] Extended chorus (64 beats) — beat 32 (halfway, mid-reroll)",
        "context": _ctx("chorus", beat_in_phrase=32, total_beats=64, beats_to_next=32,
                        next_phrase="bridge", bpm=128.0,
                        current_effect="StrobeStorm", current_palette="Cyberpunk", time_in_effect=15.0),
    },
    {
        "name": "[ENERGY] Extended chorus (64 beats) — beat 48 (closing stretch)",
        "context": _ctx("chorus", beat_in_phrase=48, total_beats=64, beats_to_next=16,
                        next_phrase="bridge", bpm=128.0,
                        current_effect="DualLaserChase", current_palette="Fire", time_in_effect=15.0),
    },
    {
        "name": "[ENERGY] Short intro (8 beats) — quick opener",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=8, beats_to_next=7,
                        next_phrase="verse", bpm=130.0,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ice"),
    },
    {
        "name": "[ENERGY] Long bridge (64 beats) — deep in atmosphere (beat 40)",
        "context": _ctx("bridge", beat_in_phrase=40, total_beats=64, beats_to_next=24,
                        next_phrase="chorus", bpm=128.0,
                        current_effect="LongBreath", current_palette="BlueOnly", time_in_effect=18.0),
    },
    {
        "name": "[ENERGY] Same effect too long — must change (18s in verse)",
        "context": _ctx("verse", beat_in_phrase=20, total_beats=32, beats_to_next=12,
                        next_phrase="chorus", bpm=128.0,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=18.5),
    },
    {
        "name": "[ENERGY] Effect just started — should stay (1.5s in chorus)",
        "context": _ctx("chorus", beat_in_phrase=4, total_beats=32, beats_to_next=28,
                        next_phrase="bridge", bpm=132.0,
                        current_effect="ImpactWaves", current_palette="Neon", time_in_effect=1.5),
    },
]

# ── GROUP 5: Direction, Grouping, Spacing, Offset, Density ───────────────────
DIRECTION_DENSITY = [
    # -- Direction: mirror --
    {
        "name": "[DD] Chorus drop — should use mirror for bilateral symmetry",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", bpm=130.0,
                        event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="Sunset",
                        time_in_effect=0.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    {
        "name": "[DD] Second chorus — mirror already on, try reversing it",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", bpm=130.0,
                        event="phrase_change_incoming",
                        current_effect="StrobeStorm", current_palette="Cyberpunk",
                        time_in_effect=0.5,
                        current_direction="mirror", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    # -- Direction: reverse --
    {
        "name": "[DD] Down-phrase — energy receding, reverse + thinning",
        "context": _ctx("down", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", bpm=124.0,
                        event="phrase_change_incoming",
                        current_effect="ImpactWaves", current_palette="Fire",
                        time_in_effect=0.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    # -- Grouping + spacing: block texture --
    {
        "name": "[DD] Bridge — sparse blocks: grouping=1, spacing=8 (airy)",
        "context": _ctx("bridge", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="ColdBreath", current_palette="BlueOnly",
                        time_in_effect=0.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    {
        "name": "[DD] Verse — chunky triplet blocks for beat texture",
        "context": _ctx("verse", beat_in_phrase=8, total_beats=32, beats_to_next=24,
                        next_phrase="chorus", bpm=128.0,
                        current_effect="LaserScan", current_palette="bro",
                        time_in_effect=3.8,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    {
        "name": "[DD] Loop chorus — tight 4-beat loop, bold block grouping",
        "context": _ctx("chorus", beat_in_phrase=4, total_beats=32, beats_to_next=None,
                        is_looping=True, loop_length=4, bpm=132.0,
                        current_effect="FlashBeat", current_palette="Neon",
                        time_in_effect=3.0,
                        current_direction="forward", current_grouping=3,
                        current_spacing=2, current_offset=0.0, current_density=1.0),
    },
    # -- Offset: phase shift --
    {
        "name": "[DD] Chorus second half — offset 0.5 for visual variety",
        "context": _ctx("chorus", beat_in_phrase=17, total_beats=32, beats_to_next=15,
                        next_phrase="bridge", bpm=128.0,
                        current_effect="DualLaserChase", current_palette="Cyberpunk",
                        time_in_effect=8.1,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    {
        "name": "[DD] Mirror + offset 0.25 — asymmetric mirror feel",
        "context": _ctx("chorus", beat_in_phrase=9, total_beats=32, beats_to_next=None,
                        is_looping=True, loop_length=8, bpm=130.0,
                        current_effect="RaveStrobe", current_palette="Fire",
                        time_in_effect=7.5,
                        current_direction="mirror", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    # -- Up phrase: build density and grouping --
    {
        "name": "[DD] Up-phrase start — sparse, forward, low density",
        "context": _ctx("up", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="BeatFlash", current_palette="Neon",
                        time_in_effect=0.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=10, current_offset=0.0, current_density=0.4),
    },
    {
        "name": "[DD] Up-phrase final bars — anticipation, pack it in",
        "context": _ctx("up", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase="chorus", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect="TripleChase", current_palette="Neon",
                        time_in_effect=12.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=3, current_offset=0.0, current_density=0.8),
    },
    # -- Intro: minimal --
    {
        "name": "[DD] Intro — very first decision, all defaults",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", bpm=128.0,
                        event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean",
                        time_in_effect=0.0,
                        current_direction="forward", current_grouping=1,
                        current_spacing=0, current_offset=0.0, current_density=1.0),
    },
    # -- Outro: wind down --
    {
        "name": "[DD] Outro — reverse + wide spacing, thinning out",
        "context": _ctx("outro", beat_in_phrase=20, total_beats=32, beats_to_next=12,
                        next_phrase=None, bpm=128.0,
                        current_effect="FogDrift", current_palette="Sunset",
                        time_in_effect=9.5,
                        current_direction="forward", current_grouping=1,
                        current_spacing=5, current_offset=0.0, current_density=0.5),
    },
    # -- Density interplay --
    {
        "name": "[DD] Verse — blocks with density thinning (g=3, s=2, density=0.7)",
        "context": _ctx("verse", beat_in_phrase=14, total_beats=32, beats_to_next=18,
                        next_phrase="chorus", bpm=132.0,
                        current_effect="MeteorShower", current_palette="DeepHouse",
                        time_in_effect=6.1,
                        current_direction="forward", current_grouping=3,
                        current_spacing=2, current_offset=0.0, current_density=0.7),
    },
]

# ── All scenarios combined ─────────────────────────────────────────────────────

SCENARIOS = FULL_SET + LOOPS + TRANSITIONS + ENERGY + DIRECTION_DENSITY

_n0 = len(FULL_SET)
_n1 = _n0 + len(LOOPS)
_n2 = _n1 + len(TRANSITIONS)
_n3 = _n2 + len(ENERGY)

SCENARIO_GROUPS = {
    "full_set":         list(range(_n0)),
    "loops":            list(range(_n0, _n1)),
    "transitions":      list(range(_n1, _n2)),
    "energy":           list(range(_n2, _n3)),
    "direction_density": list(range(_n3, len(SCENARIOS))),
    "all":              list(range(len(SCENARIOS))),
}


# ── Runner ─────────────────────────────────────────────────────────────────────

def run(api_key, group="all"):
    from light_ai import LightAI

    ai = LightAI(api_key=api_key, system_prompt_path=SYSTEM_PROMPT_PATH)

    indices = SCENARIO_GROUPS.get(group, SCENARIO_GROUPS["all"])

    print(f"\n{'='*65}")
    print(f"  DJ LightAI extended test — {len(indices)} scenarios  [{group}]")
    print(f"{'='*65}\n")

    passed = 0
    failed = 0

    for i in indices:
        scenario = SCENARIOS[i]
        name     = scenario["name"]
        context  = dict(scenario["context"])

        print(f"[{i+1}/{len(SCENARIOS)}] {name}")
        print(f"  phrase={context['phrase_type']:<8}  "
              f"beat={context['beat_in_phrase']:>3}/{context['total_beats_in_phrase']:<3}  "
              f"bpm={context['bpm']:.0f}  "
              f"loop={context['is_looping']}  "
              f"t_eff={context['time_in_current_effect']:.1f}s")

        t0       = time.perf_counter()
        decision = ai.decide(context)
        elapsed  = time.perf_counter() - t0

        if decision.get("action") == "hold" and "error" in decision.get("reason", "").lower():
            print(f"  FALLBACK  reason={decision.get('reason')}  ({elapsed:.2f}s)\n")
            failed += 1
            continue

        action    = decision.get("action", "?")
        effect    = decision.get("effect", "—")
        palette   = decision.get("palette", "—")
        bright    = decision.get("brightness")
        speed     = decision.get("speed")
        direction = decision.get("direction")
        grouping  = decision.get("grouping")
        spacing   = decision.get("spacing")
        offset    = decision.get("offset")
        density   = decision.get("density")
        reason    = decision.get("reason", "")

        bright_s   = f"{bright:.0%}"   if bright    is not None else "—"
        speed_s    = f"{speed:.1f}x"   if speed     is not None else "—"
        dir_s      = direction         if direction             else "—"
        group_s    = str(grouping)     if grouping  is not None else "—"
        spacing_s  = str(spacing)      if spacing   is not None else "—"
        offset_s   = f"{offset:.2f}"   if offset    is not None else "—"
        density_s  = f"{density:.0%}"  if density   is not None else "—"

        print(f"  {action.upper():<5}  eff={effect}  pal={palette}  "
              f"bright={bright_s}  speed={speed_s}")
        print(f"         dir={dir_s}  g={group_s}+s={spacing_s}  "
              f"off={offset_s}  density={density_s}")
        print(f"  reason: {reason}")

        if action == "change":
            avail_e = context.get("available_effects", [])
            avail_p = context.get("available_palettes", [])
            if effect and avail_e and effect not in avail_e:
                print(f"  WARN: effect '{effect}' not in available list")
            if palette and avail_p and palette not in avail_p:
                print(f"  WARN: palette '{palette}' not in available list")

        print(f"  ({elapsed:.2f}s)\n")
        passed += 1

    print(f"{'='*65}")
    print(f"  Results: {passed} passed, {failed} failed  "
          f"({len(indices)} scenarios total)")
    print(f"{'='*65}\n")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extended LightAI test")
    parser.add_argument("--key",   default=os.getenv("ANTHROPIC_API_KEY"),
                        help="Anthropic API key (default: $ANTHROPIC_API_KEY)")
    parser.add_argument("--group", default="all",
                        choices=list(SCENARIO_GROUPS.keys()),
                        help="Scenario group to run (default: all) — "
                             "options: full_set, loops, transitions, energy, direction_density, all")
    args = parser.parse_args()

    if not args.key:
        print("Error: provide --key or set ANTHROPIC_API_KEY env var")
        sys.exit(1)

    run(args.key, args.group)


if __name__ == "__main__":
    main()
