"""
test_ai.py — Offline test for LightAI decision making.

Usage:
    python test_ai.py                          # uses ANTHROPIC_API_KEY env var
    python test_ai.py --key sk-ant-...         # explicit key
    python test_ai.py --scenario loop          # run only loop scenarios
"""
import argparse
import json
import os
import sys
import time

SYSTEM_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "light_ai.md")

# ── Fake phrase structure ─────────────────────────────────────────────────────

def _ctx(phrase_type, beat_in_phrase=1, total_beats=32, beats_to_next=28,
         next_phrase=None, bpm=128.0, beat_within_bar=1, is_looping=False,
         is_master=True, current_effect=None, current_palette="Ocean",
         time_in_effect=2.0, event=None, loop_length=None,
         available_effects=None, available_palettes=None):
    """Build a context dict matching what the app sends to the AI."""
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
    return {
        "phrase_type":           phrase_type,
        "beat_in_phrase":        beat_in_phrase,
        "total_beats_in_phrase": total_beats,
        "beats_to_next_phrase":  beats_to_next,
        "next_phrase_type":      next_phrase,
        "bpm":                   bpm,
        "beat_within_bar":       beat_within_bar,
        "is_looping":            is_looping,
        "is_master":             is_master,
        "current_effect":        current_effect,
        "current_palette":       current_palette,
        "time_in_current_effect": time_in_effect,
        "event":                 event,
        "loop_length":           loop_length,
        "available_effects":     available_effects or PHRASE_EFFECTS.get(phrase_type, []),
        "available_palettes":    available_palettes or PHRASE_PALETTES.get(phrase_type, []),
    }


# ── Scenarios ─────────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "name": "Track start — intro, beat 1",
        "context": _ctx("intro", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="verse", event="phrase_change_incoming",
                        current_effect=None, current_palette="Ocean"),
    },
    {
        "name": "Intro → verse approaching (6 beats out)",
        "context": _ctx("intro", beat_in_phrase=26, total_beats=32, beats_to_next=6,
                        next_phrase="verse", event="phrase_change_incoming",
                        current_effect="NightSky", current_palette="Ocean", time_in_effect=12.0),
    },
    {
        "name": "Verse, mid-phrase (beat 12)",
        "context": _ctx("verse", beat_in_phrase=12, total_beats=32, beats_to_next=20,
                        next_phrase="chorus", current_effect="LaserScan",
                        current_palette="bro", time_in_effect=6.0),
    },
    {
        "name": "Verse → chorus approaching (4 beats out)",
        "context": _ctx("verse", beat_in_phrase=28, total_beats=32, beats_to_next=4,
                        next_phrase="chorus", event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="bro", time_in_effect=14.0),
    },
    {
        "name": "Chorus drop — beat 1",
        "context": _ctx("chorus", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="bridge", event="phrase_change_incoming",
                        current_effect="PulseDuo", current_palette="bro", time_in_effect=0.5),
    },
    {
        "name": "Chorus, 4-bar reroll (beat 17)",
        "context": _ctx("chorus", beat_in_phrase=17, total_beats=32, beats_to_next=15,
                        next_phrase="bridge", current_effect="RaveStrobe",
                        current_palette="Neon", time_in_effect=16.5),
    },
    {
        "name": "Bridge — atmospheric entry",
        "context": _ctx("bridge", beat_in_phrase=1, total_beats=32, beats_to_next=31,
                        next_phrase="chorus", event="phrase_change_incoming",
                        current_effect="StrobeStorm", current_palette="Fire", time_in_effect=0.5),
    },
    {
        "name": "4-beat loop activated (verse)",
        "context": _ctx("verse", beat_in_phrase=8, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=4,
                        current_effect="LaserScan", current_palette="bro", time_in_effect=4.0),
    },
    {
        "name": "8-beat loop — refresh at beat 8",
        "context": _ctx("chorus", beat_in_phrase=8, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=8,
                        current_effect="FlashBeat", current_palette="Neon", time_in_effect=8.0),
    },
    {
        "name": "2-beat loop (tight, should stay)",
        "context": _ctx("verse", beat_in_phrase=2, total_beats=32, beats_to_next=None,
                        is_looping=True, event="loop_active", loop_length=2,
                        current_effect="BeatFlash", current_palette="bro", time_in_effect=1.5),
    },
    {
        "name": "Outro — wind down",
        "context": _ctx("outro", beat_in_phrase=4, total_beats=32, beats_to_next=28,
                        next_phrase=None, current_effect="RaveStrobe",
                        current_palette="Cyberpunk", time_in_effect=16.0),
    },
]

SCENARIO_GROUPS = {
    "normal":  [0, 1, 2, 3, 4, 5, 6, 10],
    "loop":    [7, 8, 9],
    "all":     list(range(len(SCENARIOS))),
}


# ── Runner ────────────────────────────────────────────────────────────────────

def run(api_key, scenario_filter="all"):
    from light_ai import LightAI

    ai = LightAI(api_key=api_key, system_prompt_path=SYSTEM_PROMPT_PATH)

    indices = SCENARIO_GROUPS.get(scenario_filter, SCENARIO_GROUPS["all"])

    print(f"\n{'='*60}")
    print(f"  DJ LightAI test — {len(indices)} scenarios")
    print(f"{'='*60}\n")

    passed = 0
    failed = 0

    for i in indices:
        scenario = SCENARIOS[i]
        name     = scenario["name"]
        context  = dict(scenario["context"])  # copy so AI can mutate available_* fields

        print(f"[{i+1}/{len(SCENARIOS)}] {name}")
        print(f"  phrase={context['phrase_type']}  beat={context['beat_in_phrase']}"
              f"  loop={context['is_looping']}  event={context.get('event')}")

        t0 = time.perf_counter()
        decision = ai.decide(context)
        elapsed  = time.perf_counter() - t0

        if decision.get("action") == "hold":
            # fallback (API error)
            print(f"  ✗ FALLBACK  reason={decision.get('reason')}  ({elapsed:.2f}s)\n")
            failed += 1
            continue

        action  = decision.get("action", "?")
        effect  = decision.get("effect", "—")
        palette = decision.get("palette", "—")
        bright  = decision.get("brightness")
        speed   = decision.get("speed")
        reason  = decision.get("reason", "")

        bright_str = f"{bright:.0%}" if bright is not None else "—"
        speed_str  = f"{speed:.1f}x" if speed  is not None else "—"

        print(f"  ✓ {action.upper():<6}  effect={effect}  palette={palette}"
              f"  brightness={bright_str}  speed={speed_str}")
        print(f"     reason: {reason}")

        # Validate: if action=change, effect and palette must be in available lists
        if action == "change":
            avail_e = context.get("available_effects", [])
            avail_p = context.get("available_palettes", [])
            if effect and avail_e and effect not in avail_e:
                print(f"  ⚠ effect '{effect}' not in available list: {avail_e}")
            if palette and avail_p and palette not in avail_p:
                print(f"  ⚠ palette '{palette}' not in available list: {avail_p}")

        print(f"     ({elapsed:.2f}s)\n")
        passed += 1

    print(f"{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test LightAI offline")
    parser.add_argument("--key",      default=os.getenv("ANTHROPIC_API_KEY"),
                        help="Anthropic API key (default: $ANTHROPIC_API_KEY)")
    parser.add_argument("--scenario", default="all",
                        choices=["all", "normal", "loop"],
                        help="Scenario group to run (default: all)")
    args = parser.parse_args()

    if not args.key:
        print("Error: provide --key or set ANTHROPIC_API_KEY env var")
        sys.exit(1)

    run(args.key, args.scenario)


if __name__ == "__main__":
    main()
