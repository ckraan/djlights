"""
Performance report for djlights timing log.

Usage:
    python perf_report.py                  # analyse latest timing log
    python perf_report.py -f               # live tail — refresh every 5s
    python perf_report.py path/to/log      # specific file
"""

import os
import re
import sys
import time
import statistics
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Regex patterns ────────────────────────────────────────────────────────────
_RE_BEAT = re.compile(
    r"BEAT\s+P(\d)\s+bn=(\d+)\s+bar=\d+\s+phrase=(\w+)\s+bip=(\d+).*?bpm=([\d.]+)"
    r".*?effect=(\S+).*?bright=([\d.]+).*?speed=([\d.]+).*?ai=(\w+)"
)
_RE_EFFECT_PLAN = re.compile(
    r"EFFECT_FROM_PLAN.*?phrase=(\w+).*?effect=(\S+).*?palette=(\S+).*?bright=([\d.]+)"
    r".*?osc_to_effect=([\d.]+)ms"
)
_RE_EFFECT_RAND = re.compile(r"EFFECT_APPLIED_RANDOM.*?phrase=(\w+).*?effect=(\S+)")
_RE_DDP = re.compile(r"DDP_SENT.*?osc_to_ddp=([\d.]+)ms")
_RE_PHRASE_DETECT = re.compile(
    r"PHRASE_DETECTED\s+([\w\-]+)→(\w+).*?osc_lag=([\d.]+)ms"
)
_RE_RENDER_DIAG = re.compile(r"RENDER_DIAG.*?beat_age=([\d.]+)s")
_RE_GLOBE_READY = re.compile(r"GLOBE_PLAN_READY.*?track=(\d+).*?phrases=(\d+).*?title=(.+)")
_RE_GLOBE_CACHED = re.compile(r"GLOBE_PLAN_CACHED.*?track=(\d+)")
_RE_GLOBE_FAILED = re.compile(r"GLOBE_PLAN_FAILED.*?error=(.+)")
_RE_PLAN_READY = re.compile(r"PLAN_READY.*?track=(\d+).*?phrases=(\d+).*?title=(.+)")
_RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3})")
_RE_HOLD = re.compile(r"HOLD_FOR_PLAN")

# ── Parser ────────────────────────────────────────────────────────────────────

def parse_log(path):
    data = {
        "beats": [],              # (time_s, player, beat_n, phrase, bip, bpm, effect, bright, speed, ai_on)
        "effects_plan": [],       # (phrase, effect, palette, bright, latency_ms)
        "effects_rand": [],       # (phrase, effect)
        "ddp_latencies": [],      # float ms
        "phrase_latencies": [],   # float ms — OSC lag at phrase change
        "beat_ages": [],          # float s — how stale last OSC beat is
        "globe_ready": [],        # (track_id, phrases, title)
        "globe_cached": [],       # track_id
        "globe_failed": [],       # error str
        "plan_ready": [],         # (track_id, phrases, title)
        "holds": 0,               # times we held for a plan (plan miss)
        "beat_intervals": [],     # inter-beat gap seconds
        "last_beat_time": None,
        "first_ts": None,
        "last_ts": None,
    }

    with open(path, errors="replace") as f:
        for line in f:
            m_ts = _RE_TIMESTAMP.match(line)
            if not m_ts:
                continue
            ts_str = m_ts.group(1)
            h, rest = ts_str.split(":", 1)
            m2, s2 = rest.split(":", 1)
            ts = int(h)*3600 + int(m2)*60 + float(s2)
            if data["first_ts"] is None:
                data["first_ts"] = ts
            data["last_ts"] = ts

            if "BEAT " in line:
                m = _RE_BEAT.search(line)
                if m:
                    player, bn, phrase, bip, bpm, effect, bright, speed, ai = m.groups()
                    t = ts
                    if data["last_beat_time"] is not None:
                        gap = t - data["last_beat_time"]
                        if 0.1 < gap < 2.0:  # sane range
                            data["beat_intervals"].append(gap)
                    data["last_beat_time"] = t
                    data["beats"].append((t, int(player), int(bn), phrase,
                                          int(bip), float(bpm), effect,
                                          float(bright), float(speed), ai == "ON"))

            elif "EFFECT_FROM_PLAN" in line:
                m = _RE_EFFECT_PLAN.search(line)
                if m:
                    phrase, effect, palette, bright, lat = m.groups()
                    data["effects_plan"].append((phrase, effect, palette,
                                                  float(bright), float(lat)))

            elif "EFFECT_APPLIED_RANDOM" in line:
                m = _RE_EFFECT_RAND.search(line)
                if m:
                    data["effects_rand"].append(m.groups())

            elif "DDP_SENT" in line:
                m = _RE_DDP.search(line)
                if m:
                    data["ddp_latencies"].append(float(m.group(1)))

            elif "PHRASE_DETECTED" in line:
                m = _RE_PHRASE_DETECT.search(line)
                if m:
                    data["phrase_latencies"].append(float(m.group(3)))

            elif "RENDER_DIAG" in line:
                m = _RE_RENDER_DIAG.search(line)
                if m:
                    data["beat_ages"].append(float(m.group(1)))

            elif "GLOBE_PLAN_READY" in line:
                m = _RE_GLOBE_READY.search(line)
                if m:
                    data["globe_ready"].append(m.groups())

            elif "GLOBE_PLAN_CACHED" in line:
                m = _RE_GLOBE_CACHED.search(line)
                if m:
                    data["globe_cached"].append(m.group(1))

            elif "GLOBE_PLAN_FAILED" in line:
                m = _RE_GLOBE_FAILED.search(line)
                if m:
                    data["globe_failed"].append(m.group(1))

            elif "PLAN_READY" in line:
                m = _RE_PLAN_READY.search(line)
                if m:
                    data["plan_ready"].append(m.groups())

            elif "HOLD_FOR_PLAN" in line:
                data["holds"] += 1

    return data


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = int(len(s) * p / 100)
    return s[min(i, len(s)-1)]


def _stats(vals, unit="ms"):
    if not vals:
        return "no data"
    return (f"p50={_pct(vals,50):.1f}{unit}  "
            f"p95={_pct(vals,95):.1f}{unit}  "
            f"max={max(vals):.1f}{unit}  "
            f"n={len(vals)}")


def report(data):
    lines = []
    w = lines.append

    dur = 0
    if data["first_ts"] and data["last_ts"]:
        dur = data["last_ts"] - data["first_ts"]
    mm, ss = divmod(int(dur), 60)
    w(f"\n{'='*60}")
    w(f"  PERFORMANCE REPORT  —  {mm}:{ss:02d} of data")
    w(f"{'='*60}")

    # ── Beat health ──────────────────────────────────────────────────────────
    beats = data["beats"]
    w(f"\n── Beat health ─────────────────────────────────────────────")
    w(f"  Total beats logged : {len(beats)}")
    intervals = data["beat_intervals"]
    if intervals:
        avg_bpm = 60.0 / statistics.mean(intervals)
        jitter_ms = statistics.stdev(intervals) * 1000 if len(intervals) > 1 else 0
        w(f"  Avg BPM            : {avg_bpm:.1f}")
        w(f"  Beat jitter (σ)    : {jitter_ms:.1f}ms  {'✓ tight' if jitter_ms < 15 else '⚠ loose'}")
        w(f"  Beat interval      : {_stats(intervals, 's')}")

    ages = data["beat_ages"]
    if ages:
        stale = sum(1 for a in ages if a > 1.0)
        w(f"  Beat age (DIAG)    : {_stats(ages, 's')}  stale_frames={stale}")

    # ── Effect latency ───────────────────────────────────────────────────────
    w(f"\n── Effect latency ──────────────────────────────────────────")
    ddp = data["ddp_latencies"]
    plan_lats = [e[4] for e in data["effects_plan"]]
    phrase_lats = data["phrase_latencies"]
    w(f"  OSC→DDP            : {_stats(ddp)}")
    w(f"  OSC→effect (plan)  : {_stats(plan_lats)}")
    w(f"  Phrase detect lag  : {_stats(phrase_lats)}")

    # ── AI plan usage ────────────────────────────────────────────────────────
    n_plan = len(data["effects_plan"])
    n_rand = len(data["effects_rand"])
    total_eff = n_plan + n_rand
    hit_pct = (n_plan / total_eff * 100) if total_eff else 0
    w(f"\n── AI plan usage ───────────────────────────────────────────")
    w(f"  From plan          : {n_plan}  ({hit_pct:.0f}%)")
    w(f"  Random fallback    : {n_rand}")
    w(f"  Holds (plan miss)  : {data['holds']}")
    w(f"  Plans built        : {len(data['plan_ready'])}")
    for tid, np_, title in data["plan_ready"]:
        w(f"    track {tid}: {np_} phrases — {title.strip()[:40]}")

    # ── Effect breakdown by phrase ────────────────────────────────────────────
    w(f"\n── Effects by phrase (plan) ────────────────────────────────")
    by_phrase = defaultdict(list)
    for phrase, effect, palette, bright, lat in data["effects_plan"]:
        by_phrase[phrase].append((effect, palette, bright, lat))
    phrase_order = ["intro", "verse", "up", "chorus", "down", "bridge", "outro"]
    for ph in phrase_order:
        items = by_phrase.get(ph, [])
        if not items:
            continue
        effs = [x[0] for x in items]
        pals = [x[1] for x in items]
        brights = [x[2] for x in items]
        lats = [x[3] for x in items]
        top_eff = max(set(effs), key=effs.count)
        top_pal = max(set(pals), key=pals.count)
        w(f"  {ph:8s}  n={len(items):3d}  "
          f"top_effect={top_eff}  top_palette={top_pal}  "
          f"avg_bright={sum(brights)/len(brights):.2f}  "
          f"lat_p95={_pct(lats,95):.0f}ms")

    # ── BPM range ────────────────────────────────────────────────────────────
    if beats:
        bpms = [b[5] for b in beats if b[5] > 0]
        if bpms:
            w(f"\n── BPM range ───────────────────────────────────────────────")
            w(f"  Min={min(bpms):.1f}  Max={max(bpms):.1f}  Avg={sum(bpms)/len(bpms):.1f}")

    # ── Globe renderer ───────────────────────────────────────────────────────
    n_gr = len(data["globe_ready"]) + len(data["globe_cached"])
    n_gf = len(data["globe_failed"])
    if n_gr + n_gf > 0:
        w(f"\n── Globe renderer (beta) ───────────────────────────────────")
        w(f"  Plans ready/cached : {n_gr}")
        w(f"  Plans failed       : {n_gf}")
        for e in data["globe_failed"]:
            w(f"    error: {e.strip()[:60]}")

    w(f"\n{'='*60}\n")
    return "\n".join(lines)


def latest_log():
    logs = sorted(
        [os.path.join(_HERE, f) for f in os.listdir(_HERE)
         if f.startswith("timing_") and f.endswith(".log")],
        key=os.path.getmtime
    )
    return logs[-1] if logs else None


if __name__ == "__main__":
    follow = "-f" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else latest_log()

    if not path or not os.path.exists(path):
        print("No timing log found.")
        sys.exit(1)

    print(f"Analysing: {os.path.basename(path)}")

    if follow:
        print("Live mode — refreshing every 5s (Ctrl-C to stop)\n")
        try:
            while True:
                data = parse_log(path)
                os.system("clear")
                print(f"Analysing: {os.path.basename(path)}")
                print(report(data))
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        data = parse_log(path)
        print(report(data))
