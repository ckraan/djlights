"""Back strip renderer — racing blocks at 255 brightness.

Dark on all phrases except up/chorus/strobe.
On beat: 2-4 thin blocks launch from edges/center, race, explode at strip ends.
Dark gaps between blocks are intentional — they make the blocks readable.
"""

import math
import time
import numpy as np

BLOCK_W      = 22   # thin enough to leave clear dark gaps
STROBE_MIN_W = 12   # min random strobe block width
STROBE_MAX_W = 55   # max random strobe block width


class CaveStripRenderer:

    def __init__(self, n_leds: int = 768):
        self._n           = n_leds
        self._last_beat_n = -1
        self._last_beat_t = -99.0
        self._beat_count  = 0
        self._last_phrase = ""
        # (launch_pos, velocity_px_per_sec, birth_t, color_flag 0=c1 1=c2)
        self._blocks: list     = []
        # (center_pos, birth_t, color_flag)
        self._explosions: list = []
        # (pos, width, birth_t, half_life, color_flag)  — random strobe blocks
        self._strobe_blocks: list = []
        self._strobe_rng = np.random.RandomState(42)

    def render(self, t: float, bpm: float, phrase: str,
               beat_n: int, beat_age: float,
               c1: np.ndarray, c2: np.ndarray,
               wf_energy: float = None,
               wf_spectral: np.ndarray = None) -> np.ndarray:

        n   = self._n
        bpm = max(bpm, 60.0)
        now = time.perf_counter()

        new_beat = beat_n != self._last_beat_n and beat_age < 0.30
        if new_beat:
            self._last_beat_n = beat_n
            self._last_beat_t = now - beat_age
            if phrase != self._last_phrase:
                self._beat_count  = 0
                self._last_phrase = phrase
                self._blocks      = []
                self._explosions  = []
            self._beat_count += 1

        if phrase not in ("up", "chorus", "strobe"):
            self._blocks       = []
            self._explosions   = []
            self._strobe_blocks = []
            return np.zeros((n, 3), dtype=np.uint8)

        # Block speed: crosses full strip in ~1.5 beats (slightly slower = more visible)
        beat_dur    = 60.0 / bpm
        block_speed = n / (beat_dur * 1.5)

        c1f = c1.astype(np.float32)
        c2f = c2.astype(np.float32)

        # ── Spawn random strobe blocks ─────────────────────────────────────────
        # Each beat: scatter 3-6 random-width blocks at random positions.
        # They flash on hard and decay quickly — independent of the racing blocks.
        # chorus gets more blocks and shorter half-life (faster strobe feel).
        if new_beat:
            n_strobe   = 5 if phrase == "chorus" else 3
            half_life  = 0.12 if phrase == "chorus" else 0.18  # decay constant
            for _ in range(n_strobe):
                sw    = int(self._strobe_rng.randint(STROBE_MIN_W, STROBE_MAX_W + 1))
                pos   = int(self._strobe_rng.randint(0, max(1, n - sw)))
                cflag = int(self._strobe_rng.randint(0, 2))
                self._strobe_blocks.append((pos, sw, now, half_life, cflag))

        # ── Launch racing blocks on each beat ──────────────────────────────────
        if new_beat:
            cx = n // 2
            bc = self._beat_count

            if phrase == "up":
                # 2 blocks from center racing out, alternate with 2 from edges racing in
                if bc % 2 == 1:
                    self._blocks += [
                        (cx,           +block_speed,       now, 1),
                        (cx - BLOCK_W, -block_speed,       now, 1),
                    ]
                else:
                    self._blocks += [
                        (0,            +block_speed * 0.8, now, 0),
                        (n - BLOCK_W,  -block_speed * 0.8, now, 0),
                    ]

            else:  # chorus / strobe — 2 from each end, spaced apart
                # Space blocks 60 LEDs apart so gaps are visible
                GAP = 60
                self._blocks += [
                    (0,             +block_speed,        now, 1),
                    (GAP,           +block_speed * 0.75, now, 0),
                    (n - BLOCK_W,   -block_speed,        now, 1),
                    (n - BLOCK_W - GAP, -block_speed * 0.75, now, 0),
                ]
                # Every other beat: extra center burst
                if bc % 2 == 0:
                    self._blocks += [
                        (cx,           +block_speed * 1.3, now, 1),
                        (cx - BLOCK_W, -block_speed * 1.3, now, 1),
                    ]

        # ── Advance, cull, draw ────────────────────────────────────────────────
        pixels    = np.zeros((n, 3), dtype=np.float32)
        surviving = []

        for (launch, vel, birth, cflag) in self._blocks:
            elapsed = now - birth
            pos     = int(launch + vel * elapsed)
            col     = c2f if cflag == 1 else c1f

            # Gone off the far end → explode
            if vel > 0 and pos >= n - 2:
                self._explosions.append((float(n - 1), now, cflag))
                continue
            if vel < 0 and pos + BLOCK_W <= 2:
                self._explosions.append((0.0, now, cflag))
                continue

            # Fade: full brightness for 0.6 beats, then fast decay
            # This ensures blocks are gone before too many accumulate
            hold = beat_dur * 0.6
            fade = max(0.0, 1.0 - max(0.0, elapsed - hold) * 6.0)
            if fade < 0.02:
                continue

            lo = max(0, pos)
            hi = min(lo + BLOCK_W, n)
            if hi > lo:
                pixels[lo:hi] = np.maximum(pixels[lo:hi], col * fade)

            surviving.append((launch, vel, birth, cflag))

        self._blocks = surviving

        # ── Explosions: radial burst from edge ─────────────────────────────────
        exp_surviving = []
        for (epos, ebirth, cflag) in self._explosions:
            eage  = now - ebirth
            efade = math.exp(-eage * 8.0)
            if efade < 0.02:
                continue
            exp_surviving.append((epos, ebirth, cflag))
            col    = c2f if cflag == 1 else c1f
            radius = int(eage * n * 1.5)
            lo     = max(0, int(epos) - radius)
            hi     = min(n, int(epos) + radius)
            if hi > lo:
                pixels[lo:hi] = np.maximum(pixels[lo:hi], col * efade)
        self._explosions = exp_surviving

        # ── Random strobe blocks ───────────────────────────────────────────────
        sb_surviving = []
        for (spos, sw, sbirth, shl, cflag) in self._strobe_blocks:
            sage  = now - sbirth
            # Hard on, exponential decay with per-block half-life
            sfade = math.exp(-sage / shl)
            if sfade < 0.02:
                continue
            sb_surviving.append((spos, sw, sbirth, shl, cflag))
            col = c2f if cflag == 1 else c1f
            lo  = max(0, spos)
            hi  = min(n, spos + sw)
            if hi > lo:
                pixels[lo:hi] = np.maximum(pixels[lo:hi], col * sfade)
        self._strobe_blocks = sb_surviving

        # ── Beat impact flash — short and localised, not full-strip ────────────
        beat_age_now = max(0.0, now - self._last_beat_t)
        # Very fast decay (15) so it dies in ~0.1s — marks the beat without
        # flooding the strip with light that kills the dark gaps
        raw_flash = math.exp(-beat_age_now * 15.0)

        if phrase in ("chorus", "strobe"):
            # Edge flare: only the outer ~15% of strip on each side
            flare_w = max(1, int(n * 0.15))
            pixels[:flare_w]  = np.maximum(pixels[:flare_w],  c2f * raw_flash)
            pixels[-flare_w:] = np.maximum(pixels[-flare_w:], c2f * raw_flash)

        if phrase == "strobe":
            s_col = c2f if (self._beat_count % 2 == 0) else c1f
            flash2 = math.exp(-beat_age_now * 25.0)
            pixels[:] = np.maximum(pixels, s_col * flash2)

        elif phrase == "up":
            # Center radial flash — stays localised
            cx    = n // 2
            burst = int(raw_flash * n * 0.3)
            lo    = max(0, cx - burst)
            hi    = min(n, cx + burst)
            if hi > lo:
                pixels[lo:hi] = np.maximum(pixels[lo:hi], c1f * raw_flash)

        return np.clip(pixels, 0, 255).astype(np.uint8)
