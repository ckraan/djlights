import base64
import customtkinter as ctk
import json
import settings as _settings
import os
import time
import urllib.request
from datetime import datetime
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_AI_CONFIG          = os.path.join(_HERE, "ai_config.json")
_IMPROVED_PLANS_FILE  = os.path.join(_HERE, "improved_plans.json")
_LEARNED_RULES_FILE   = os.path.join(_HERE, "learned_rules.json")


def _load_improved_plans():
    try:
        with open(_IMPROVED_PLANS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_improved_plans(data):
    try:
        with open(_IMPROVED_PLANS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _load_learned_rules():
    try:
        with open(_LEARNED_RULES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_learned_rules(data):
    try:
        with open(_LEARNED_RULES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

_PHRASE_COLORS = {
    "intro": "#2d88f5", "verse": "#2dd45e", "up": "#f5b830",
    "chorus": "#f53030", "bridge": "#b830f5", "outro": "#4d88cc",
    "down": "#4d88cc", "": "#444444",
}


def _load_ai_config():
    try:
        with open(_AI_CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ai_config(data):
    try:
        with open(_AI_CONFIG, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class AILightControl(ctk.CTkFrame):
    """AI-driven lighting control tab — always-on track plan engine."""

    def __init__(self, master, wled_manager, palette_manager, effect_manager,
                 on_active_change=None):
        super().__init__(master)

        self.wled = wled_manager
        self.palettes = palette_manager
        self.effects = effect_manager
        self._on_active_change = on_active_change

        self._ai = None
        self.plan_notify_cb = None    # set by app to push plan-ready events to CDJ Monitor
        self._track_plans = {}        # track_id → {phrase_index: entry}
        self._track_phrases = {}      # track_id → phrases list (for table re-render)
        self._track_cues = {}         # track_id → list of cue dicts
        self._track_titles = {}       # track_id → title string
        self._track_artists = {}      # track_id → artist string
        self._track_bpms = {}         # track_id → bpm float
        self._planning_tids = set()        # in-flight guard (track_id)
        self._planning_n_phrases = {}      # track_id → n_phrases when plan was started
        self._planning_gen = {}            # track_id → generation counter (stale-result guard)
        self._plan_fail_times = {}    # track_id → time of last failure (for retry backoff)
        self._improved_plans  = _load_improved_plans()   # track_id(str) → plan_dict, persisted
        self._learned_rules   = _load_learned_rules()    # accumulated cross-session rules
        self._session_data    = {}    # track_id → {"beats": [...], "title": str, "latencies": [...]}
        self._effect_latencies = []  # [(track_id, phrase_kind, latency_ms), ...]
        self._session_effects_used  = []  # effects used this session (last 40), for variety hint
        self._session_palettes_used = []  # palettes used this session (last 20), for variety hint

        # Beta: globe-specific planner (additive — does not affect existing plan system)
        self._globe_planner = None   # initialised in _init_ai when AI is ready

        # Beta: vault strip planner (additive — does not affect existing plan system)
        self._vault_planner = None   # initialised in _init_ai when AI is ready

        # Per-deck state
        self._deck_track_ids = {}              # player_num → track_id
        self._deck_plan_rows = {n: {} for n in range(1, 4)}   # player_num → {phrase_idx: row}
        self._deck_plan_scroll = {}            # player_num → CTkScrollableFrame
        self._deck_plan_status_lbl = {}        # player_num → CTkLabel
        self._deck_rendered = set()            # (player_num, track_id) — prevents double renders
        self._deck_now_playing_lbl = {}        # player_num → CTkLabel ("NOW: phrase N / kind")
        self._current_committed_player = None

        # Track analysis (waveform vision)
        self._track_analysis_cache = {}   # track_id → analysis text (only stored if duration > 0)
        self._track_duration_cache = {}   # track_id → best known duration_ms
        self._track_analysis = None
        self._analyzing_tids = set()

        # UI state (legacy — also used to track last committed highlight)
        self._current_tid = None
        self._current_phrase_idx = None
        self._current_phrase_player = None

        self._build_ui()

        # Auto-initialize if API key saved
        if _load_ai_config().get("api_key"):
            self.after(100, self._init_ai)

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def _ai_ready(self):
        return self._ai is not None

    def plan_track(self, track_id, phrases, title, artist, bpm, duration_ms,
                   player_num=None, cues=None):
        """Fire async API call to plan the full track.

        Called from bg thread in wled_ctrl when track + phrases are available.
        phrases: list of {phrase_index, kind, beat_start, beat_end, duration_beats}
        player_num: which deck loaded this track (1-4), used for tab routing.
        """
        if not self._ai:
            return

        # Store metadata for this deck
        self._track_phrases[track_id] = phrases
        self._track_titles[track_id]  = (title or f"track {track_id}")[:40]
        self._track_artists[track_id] = artist or ""
        self._track_bpms[track_id]    = bpm or 0
        if cues:
            self._track_cues[track_id] = cues
            # If a plan is already rendered for this track, invalidate so it re-renders with cues
            self._deck_rendered = {k for k in self._deck_rendered if k[1] != track_id}

        # Use improved plan if one exists for this track (from a previous session)
        improved = self._improved_plans.get(str(track_id))
        if improved and not self._track_plans.get(track_id):
            self._track_plans[track_id] = improved
            n = len(improved)
            self._log(f"IMPROVED_PLAN_LOADED  track={track_id}  phrases={n}  title={title}")
            def _fire_improved(tid=track_id, ph=phrases, pl=improved, pnum=player_num,
                               nn=n, lbl=title or f"track {track_id}"):
                self._on_plan_ready(pnum, tid, ph, pl, lbl, nn)
            self.after(0, _fire_improved)
            return

        # Skip if already planning this track — UNLESS the in-flight plan was started with
        # 0 phrases and we now have real phrases (phrases arrived after plan was kicked off).
        if track_id in self._planning_tids:
            if phrases and self._planning_n_phrases.get(track_id, 0) == 0:
                # Evict the stale no-phrase plan and replan with real data
                self._planning_tids.discard(track_id)
            else:
                return

        self._planning_tids.add(track_id)
        self._planning_n_phrases[track_id] = len(phrases)
        gen = self._planning_gen.get(track_id, 0) + 1
        self._planning_gen[track_id] = gen
        track_label = self._track_titles[track_id]
        if player_num and player_num in self._deck_plan_status_lbl:
            self.after(0, lambda pn=player_num, lbl=track_label:
                self._deck_plan_status_lbl[pn].configure(
                    text=f"Planning {lbl}…", text_color="orange"))

        # Build effects/palettes per phrase kind (fall back to all kinds if no phrases)
        phrase_kinds = list(dict.fromkeys(p["kind"] for p in phrases)) if phrases else \
                       ["intro", "verse", "up", "chorus", "down", "bridge", "outro"]
        available_effects  = {k: self.effects.get_available_for_phrase(k) for k in phrase_kinds}
        available_palettes = {k: self.palettes.get_available_for_phrase(k) for k in phrase_kinds}

        def _do_plan(my_gen=gen):
            try:
                # Wait up to 8s for waveform analysis — then proceed without it.
                for _ in range(16):
                    if track_id in self._track_analysis_cache:
                        break
                    if track_id not in self._analyzing_tids:
                        break  # not being analyzed (no URL or already failed)
                    time.sleep(0.5)
                track_analysis = self._track_analysis_cache.get(track_id)

                # Inject learned rules and session effect history for variety
                rules_text = self._learned_rules.get("rules_text", "")
                enhanced_analysis = track_analysis or ""
                if rules_text:
                    enhanced_analysis = (
                        f"{enhanced_analysis}\n\n"
                        f"## Learned rules from previous sessions:\n{rules_text}"
                    ).strip()
                if self._session_effects_used:
                    recent = ", ".join(self._session_effects_used[-20:])
                    enhanced_analysis = (
                        f"{enhanced_analysis}\n\n"
                        f"## Effects already used this session (AVOID repeating these):\n{recent}\n"
                        f"Pick fresh effects not in this list to keep the show varied."
                    ).strip()
                if self._session_palettes_used:
                    recent_p = ", ".join(self._session_palettes_used[-12:])
                    enhanced_analysis = (
                        f"{enhanced_analysis}\n\n"
                        f"## Palettes recently used (AVOID using the same ones again — rotate to fresh colours):\n{recent_p}"
                    ).strip()

                plan = self._ai.plan_track(
                    phrases=phrases,
                    available_effects=available_effects,
                    available_palettes=available_palettes,
                    title=title, artist=artist, bpm=bpm, duration_ms=duration_ms,
                    track_analysis=enhanced_analysis or None,
                    cues=cues,
                )
                # Stale-result guard: if a newer plan was kicked off for this track, discard this result
                if self._planning_gen.get(track_id) != my_gen:
                    return
                # Sort by AI's phrase_index, reindex 0-based to match snap.phrases positions
                plan_sorted = sorted(plan, key=lambda e: e.get("phrase_index", 999))
                plan_dict = {i: entry for i, entry in enumerate(plan_sorted)}
                # Track session effect/palette usage for variety hints on next track
                for _e in plan_dict.values():
                    _eff = _e.get("effect", "")
                    if _eff and _eff not in self._session_effects_used:
                        self._session_effects_used.append(_eff)
                    _pal = _e.get("palette", "")
                    if _pal:
                        self._session_palettes_used.append(_pal)
                self._session_effects_used  = self._session_effects_used[-40:]
                self._session_palettes_used = self._session_palettes_used[-20:]
                # Always overwrite — fresh plan every load
                self._track_plans[track_id] = plan_dict
                self._planning_tids.discard(track_id)
                self._planning_n_phrases.pop(track_id, None)
                n = len(plan_dict)
                pn = player_num
                def _fire_ready(tid=track_id, ph=phrases, pl=plan_dict, pnum=pn, nn=n,
                                lbl_fallback=track_label):
                    # Use freshest title — may have arrived via UDP meta after plan was requested
                    lbl = self._track_titles.get(tid) or lbl_fallback
                    self._on_plan_ready(pnum, tid, ph, pl, lbl, nn)
                self.after(0, _fire_ready)
            except Exception as e:
                if self._planning_gen.get(track_id) == my_gen:
                    self._planning_tids.discard(track_id)
                    self._planning_n_phrases.pop(track_id, None)
                    self._plan_fail_times[track_id] = time.time()
                err_s = str(e)[:60]
                pn = player_num
                self.after(0, lambda es=err_s, tid=track_id, pnum=pn: (
                    (self._deck_plan_status_lbl[pnum].configure(
                        text=f"Plan failed: {es}", text_color="red")
                     if pnum and pnum in self._deck_plan_status_lbl else None),
                    self._log(f"PLAN_FAILED  track={tid}  deck=P{pnum or '?'}  error={es}"),
                ))

        threading.Thread(target=_do_plan, daemon=True).start()

        # Beta: also request a globe plan in parallel (additive — no effect if globe planner absent)
        if self._globe_planner:
            track_analysis = self._track_analysis_cache.get(track_id)
            self._globe_planner.plan_async(
                track_id, phrases, title, artist, bpm, duration_ms,
                cues=cues, track_analysis=track_analysis)

        # Beta: also request a vault plan in parallel (additive — no effect if vault planner absent)
        if self._vault_planner:
            track_analysis = self._track_analysis_cache.get(track_id)
            self._vault_planner.plan_async(
                track_id, phrases, title, artist, bpm, duration_ms,
                cues=cues, track_analysis=track_analysis)

    def get_plan_for_phrase(self, track_id, phrase_idx):
        """Returns plan entry dict for phrase_idx, or None if not ready.
        If phrase_idx is past the end of the plan, returns the last entry
        so the track doesn't go dark or repeat stale defaults."""
        plan = self._track_plans.get(track_id)
        if not plan:
            return None
        entry = plan.get(phrase_idx)
        if entry is not None:
            return entry
        # Overrun: phrase_idx beyond plan — return last planned entry
        if plan:
            last_key = max(plan.keys())
            return plan[last_key]
        return None

    def check_mid_phrase(self, track_id, phrase_idx, beat_in_phrase):
        """Returns mid-phrase change dict scheduled at beat_in_phrase, or None."""
        plan = self._track_plans.get(track_id)
        if not plan:
            return None
        entry = plan.get(phrase_idx)
        if not entry:
            return None
        phrases = self._track_phrases.get(track_id, [])
        phrase_dur = 32  # fallback
        if phrase_idx < len(phrases):
            p = phrases[phrase_idx]
            phrase_dur = p.get("duration_beats", 32)
        for change in entry.get("mid_phrase_changes", []):
            if change.get("at_beat") != beat_in_phrase:
                continue
            # Guard: darkness/blackout changes only allowed in last 4 beats of phrase
            bright = change.get("brightness")
            if bright is not None and bright < 0.25:
                if beat_in_phrase < phrase_dur - 4:
                    continue  # too early — skip this darkness change
            return change
        return None

    def set_current_phrase(self, track_id, phrase_idx, kind, player_num=None):
        """Update UI plan table to highlight current phrase. Thread-safe."""
        if not track_id:
            return
        self.after(0, lambda: self._update_deck_highlight(player_num, track_id, phrase_idx))

    def record_effect_applied(self, track_id, phrase_kind, latency_ms, effect_name):
        """Called when an effect is applied from plan — records timing quality."""
        self._effect_latencies.append({
            "track_id": track_id, "kind": phrase_kind,
            "latency_ms": round(latency_ms, 1), "effect": effect_name,
        })
        if track_id and track_id in self._session_data:
            self._session_data[track_id].setdefault("latencies", []).append(round(latency_ms, 1))

    def record_beat(self, track_id, beat_n, phrase, effect, brightness, speed, beat_in_phrase):
        """Called on every beat to accumulate session data for later improvement."""
        if not track_id:
            return
        if track_id not in self._session_data:
            self._session_data[track_id] = {
                "title":  self._track_titles.get(track_id, ""),
                "artist": self._track_artists.get(track_id, ""),
                "beats":  [],
            }
        self._session_data[track_id]["beats"].append({
            "beat_n": beat_n, "phrase": phrase, "effect": effect,
            "brightness": round(brightness, 3), "speed": round(speed, 3),
            "bip": beat_in_phrase,
        })

    def _summarize_session(self, track_id):
        """Compress beat-level session data into per-phrase summary for improve prompt."""
        data = self._session_data.get(track_id)
        if not data or not data["beats"]:
            return []
        plan = self._track_plans.get(track_id, {})
        # Group beats by phrase kind
        from collections import defaultdict
        phrase_beats = defaultdict(list)
        for b in data["beats"]:
            phrase_beats[(b["bip"] == 0, b["phrase"])].append(b)  # group by phrase entry

        # Build per-plan-entry summary
        summary = []
        # Group by contiguous phrase segments
        beats = data["beats"]
        segments = []
        cur_phrase = None
        cur_segment = []
        for b in beats:
            if b["phrase"] != cur_phrase:
                if cur_segment:
                    segments.append(cur_segment)
                cur_phrase = b["phrase"]
                cur_segment = [b]
            else:
                cur_segment.append(b)
        if cur_segment:
            segments.append(cur_segment)

        for i, seg in enumerate(segments):
            if not seg:
                continue
            brs = [b["brightness"] for b in seg]
            sps = [b["speed"] for b in seg]
            effects = {}
            for b in seg:
                effects[b["effect"]] = effects.get(b["effect"], 0) + 1
            dominant_effect = max(effects, key=effects.get) if effects else "—"
            # Find matching plan entry
            phrase = seg[0]["phrase"]
            plan_entry = None
            for pe in plan.values():
                if pe.get("kind") == phrase:
                    plan_entry = pe
                    break
            summary.append({
                "segment": i,
                "kind": phrase,
                "beats_recorded": len(seg),
                "dominant_effect": dominant_effect,
                "planned_brightness": plan_entry.get("brightness") if plan_entry else None,
                "avg_brightness": round(sum(brs) / len(brs), 3),
                "min_brightness": round(min(brs), 3),
                "max_brightness": round(max(brs), 3),
                "planned_speed": plan_entry.get("speed") if plan_entry else None,
                "avg_speed": round(sum(sps) / len(sps), 3),
                "min_speed": round(min(sps), 3),
                "max_speed": round(max(sps), 3),
                "beats_with_change": sum(1 for b in seg if b["bip"] > 0 and b["bip"] % 8 == 0),
            })
        return summary

    def _build_session_telemetry(self):
        """Compile full session telemetry across all tracks for the learn call."""
        from collections import defaultdict

        all_beats = []
        for tid, data in self._session_data.items():
            all_beats.extend(data.get("beats", []))

        if not all_beats:
            return {}

        # Per phrase kind stats
        kind_stats = defaultdict(lambda: {"brightnesses": [], "speeds": [], "effects": defaultdict(int), "beats": 0})
        for b in all_beats:
            k = b["phrase"] or "unknown"
            kind_stats[k]["brightnesses"].append(b["brightness"])
            kind_stats[k]["speeds"].append(b["speed"])
            kind_stats[k]["effects"][b["effect"]] += 1
            kind_stats[k]["beats"] += 1

        phrase_kind_stats = {}
        for k, s in kind_stats.items():
            brs = s["brightnesses"]
            sps = s["speeds"]
            top_effects = sorted(s["effects"].items(), key=lambda x: -x[1])[:3]
            phrase_kind_stats[k] = {
                "total_beats": s["beats"],
                "avg_brightness": round(sum(brs) / len(brs), 3),
                "min_brightness": round(min(brs), 3),
                "max_brightness": round(max(brs), 3),
                "avg_speed":      round(sum(sps) / len(sps), 3),
                "min_speed":      round(min(sps), 3),
                "max_speed":      round(max(sps), 3),
                "top_effects":    [{"effect": e, "beats": n} for e, n in top_effects],
            }

        # Timing stats
        lats = [l["latency_ms"] for l in self._effect_latencies]
        timing_stats = {}
        if lats:
            timing_stats = {
                "effect_apply_avg_ms": round(sum(lats) / len(lats), 1),
                "effect_apply_max_ms": round(max(lats), 1),
                "effect_apply_min_ms": round(min(lats), 1),
                "samples": len(lats),
            }

        # Effect usage across whole session
        all_effects = defaultdict(int)
        for b in all_beats:
            if b["effect"] and b["effect"] != "—":
                all_effects[b["effect"]] += 1
        top_overall = sorted(all_effects.items(), key=lambda x: -x[1])[:10]

        # Up ramp quality — did speed actually increase during up phrases?
        up_ramp_quality = []
        for tid, data in self._session_data.items():
            beats = data.get("beats", [])
            in_up = []
            for b in beats:
                if b["phrase"] == "up":
                    in_up.append(b)
                elif in_up:
                    # Analyse this up segment
                    if len(in_up) >= 8:
                        start_sp = in_up[0]["speed"]
                        end_sp   = in_up[-1]["speed"]
                        up_ramp_quality.append({
                            "beats": len(in_up),
                            "speed_start": round(start_sp, 2),
                            "speed_end":   round(end_sp, 2),
                            "speed_delta": round(end_sp - start_sp, 2),
                            "ramped": end_sp > start_sp + 0.1,
                        })
                    in_up = []

        # BPM range
        bpms = [self._track_bpms[t] for t in self._session_data if self._track_bpms.get(t)]

        return {
            "session_stats": {
                "tracks_played": len(self._session_data),
                "total_beats": len(all_beats),
                "bpm_range": [round(min(bpms), 1), round(max(bpms), 1)] if bpms else [],
            },
            "phrase_kind_stats": phrase_kind_stats,
            "timing_stats": timing_stats,
            "effect_usage_overall": [{"effect": e, "beats": n} for e, n in top_overall],
            "up_ramp_quality": up_ramp_quality[:10],  # cap to avoid huge payloads
            "sessions_history": {
                "sessions_count":     self._learned_rules.get("sessions_count", 0),
                "previous_rules":     self._learned_rules.get("rules_text", ""),
                "previous_style":     self._learned_rules.get("style_notes", ""),
            },
        }

    def set_committed_player(self, player_num):
        """Called when committed player changes — switch tab to that deck."""
        prev = self._current_committed_player
        self._current_committed_player = player_num
        tid = self._deck_track_ids.get(player_num, 0)
        self._log(f"COMMITTED_DECK  P{prev or '?'}→P{player_num}  "
                  f"track={tid}  "
                  f"has_plan={'YES' if tid in self._track_plans else 'NO'}")
        if player_num and hasattr(self, '_deck_tabview'):
            try:
                target_tab = f"P{player_num}"
                self.after(0, lambda t=target_tab: (
                    self._deck_tabview.set(t)
                    if self._deck_tabview.get() != t else None))
            except Exception:
                pass

    def _on_plan_ready(self, player_num, track_id, phrases, plan, lbl, n):
        """Called on main thread when plan arrives. Guards against stale deck state."""
        # If deck has since moved to a different track, don't overwrite the table
        if player_num and self._deck_track_ids.get(player_num) != track_id:
            self._log(f"PLAN_STALE  track={track_id}  deck=P{player_num}  "
                      f"current={self._deck_track_ids.get(player_num)}  skipping render")
            return
        # Waveform-only mode: AI synthesized phrase structure (beat_start/beat_end in plan entries).
        # Build synthetic phrases so the render loop can do beat-position tracking.
        if not phrases and plan:
            sorted_entries = sorted(plan.values(), key=lambda e: e.get("beat_start", 0))
            synth = []
            for i, e in enumerate(sorted_entries):
                bs = e.get("beat_start", 0)
                be = e.get("beat_end", bs + 32)
                if be <= bs:
                    be = bs + 32
                synth.append({
                    "phrase_index": i,
                    "kind": e.get("kind", "verse"),
                    "beat_start": bs,
                    "beat_end": be,
                    "duration_beats": be - bs,
                })
            self._track_phrases[track_id] = synth
            self._log(f"SYNTH_PHRASES  track={track_id}  n={len(synth)}  (waveform-only mode)")
            phrases = synth
        if player_num:
            self._render_deck_plan_table(player_num, track_id, phrases, plan)
        if player_num and player_num in self._deck_plan_status_lbl:
            self._deck_plan_status_lbl[player_num].configure(
                text=f"Plan ready — {lbl} ({n} phrases)", text_color="green")
        self._log(f"PLAN_READY  track={track_id}  phrases={n}  deck=P{player_num or '?'}  title={lbl}")
        # Push plan to CDJ Monitor plan panels
        if self.plan_notify_cb:
            try:
                self.plan_notify_cb(track_id)
            except Exception:
                pass

    def notify_track_loaded(self, waveform_url, track_id, title, artist, duration_ms,
                            player_num=None):
        """Called by wled_ctrl when a new track loads. Triggers waveform analysis."""
        # Keep freshest metadata — called multiple times as URL/phrases/title arrive
        if title:
            self._track_titles[track_id]  = title[:40]
        if artist:
            self._track_artists[track_id] = artist
        if duration_ms and duration_ms > 0:
            self._track_duration_cache[track_id] = duration_ms
        cached_analysis = self._track_analysis_cache.get(track_id)
        self._track_analysis = cached_analysis
        track_label = (title or f"track {track_id}")[:40]
        # Only update analysis UI if this deck's tab is currently selected
        def _update_analysis_label(lbl=track_label, a=cached_analysis, pn=player_num):
            tab = self._deck_tabview.get() if hasattr(self, '_deck_tabview') else None
            active_pnum = int(tab[1]) if (tab and len(tab) > 1) else None
            if pn and active_pnum and pn != active_pnum:
                return  # user is viewing a different deck — don't clobber their view
            self._analysis_status_lbl.configure(text=lbl, text_color="gray")
            if a:
                self._update_track_analysis_ui(a)
            else:
                # Clear stale text from previous track
                self._analysis_text.configure(state="normal")
                self._analysis_text.delete("1.0", "end")
                self._analysis_text.configure(state="disabled")
        self.after(0, _update_analysis_label)
        # Update deck track ID and invalidate render cache
        if player_num:
            old_tid = self._deck_track_ids.get(player_num)
            self._deck_track_ids[player_num] = track_id
            track_changed = old_tid != track_id
            def _on_new_track(pn=player_num, tid=track_id, lbl=track_label,
                              changed=track_changed):
                if pn in self._deck_plan_status_lbl:
                    self._deck_plan_status_lbl[pn].configure(
                        text=f"Loading — {lbl}…", text_color="gray")
                if changed:
                    self._deck_rendered = {k for k in self._deck_rendered if k[0] != pn}
                    self._deck_plan_rows[pn].clear()
                    # Defer widget destruction to next tick — avoids blocking current frame
                    scroll = self._deck_plan_scroll.get(pn)
                    if scroll:
                        children = list(scroll.winfo_children())
                        def _clear(c=children):
                            for w in c:
                                try:
                                    w.destroy()
                                except Exception:
                                    pass
                        self.after(1, _clear)
            self.after(0, _on_new_track)
        if self._ai and waveform_url:
            threading.Thread(
                target=lambda: self.analyze_track(waveform_url, track_id, title, artist, duration_ms),
                daemon=True
            ).start()

    def get_track_context(self):
        """Legacy: returns track_analysis for any code still calling this."""
        return {"track_analysis": self._track_analysis}

    # ── Waveform analysis (vision) ──────────────────────────────────────────

    def analyze_track(self, waveform_url, track_id, title, artist, duration_ms):
        """Fetch waveform image, send to Claude vision, cache result. Run in bg thread."""
        if not self._ai or not waveform_url:
            return
        if track_id in self._track_analysis_cache:
            self._track_analysis = self._track_analysis_cache[track_id]
            def _show_cached(a=self._track_analysis, tid=track_id):
                tab = self._deck_tabview.get() if hasattr(self, '_deck_tabview') else None
                active_pnum = int(tab[1]) if (tab and len(tab) > 1) else None
                active_tid = self._deck_track_ids.get(active_pnum) if active_pnum else None
                if active_tid == tid or active_tid is None:
                    self._update_track_analysis_ui(a)
            self.after(0, _show_cached)
            return
        if track_id in self._analyzing_tids:
            return
        self._analyzing_tids.add(track_id)
        self.after(0, lambda: self._analysis_status_lbl.configure(
            text="analyzing…", text_color="orange"))
        try:
            with urllib.request.urlopen(waveform_url, timeout=6) as resp:
                img_data = resp.read()
            img_b64 = base64.standard_b64encode(img_data).decode("utf-8")
            # Use best-known duration — may have arrived via UDP meta after thread was started
            best_dur = self._track_duration_cache.get(track_id) or duration_ms or 0
            dur_s = best_dur / 1000
            track_str = f'"{title}" by {artist}' if (title and artist) else (title or "unknown track")
            if dur_s > 0:
                dur_str = f"{int(dur_s // 60)}:{int(dur_s % 60):02d}"
                timing_instr = (f"Use timestamps (e.g. 'drop at 1:23') based on the "
                                f"{dur_str} total duration.")
            else:
                timing_instr = ("Use relative positions like 'first third', 'midpoint', "
                                "'final quarter' since exact duration is unavailable.")
            response = self._ai.client.messages.create(
                model=_settings.get("ai.fast_model", "claude-haiku-4-5"),
                max_tokens=700,
                timeout=20.0,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                            "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": (
                            f"This is a DJ track waveform for {track_str}. "
                            f"{timing_instr} "
                            "Analyse it for a lighting engineer. Output:\n\n"
                            "1. ENERGY MAP — list every distinct section with timestamp range, "
                            "energy level (low/build/medium/high/peak/drop/breakdown), "
                            "and a numeric intensity 0–10. Include every significant change. "
                            "Example: '0:00-0:28: low (2) — sparse kick intro; "
                            "0:28-1:05: build (6) — rising hats and bass; "
                            "1:05-1:08: drop (9) — full kick+bass hit; "
                            "1:08-2:12: peak (8) — sustained chorus'\n\n"
                            "2. KEY MOMENTS — list timestamps of drops, breakdowns, "
                            "unexpected silences, or peaks that a lighting engineer must know.\n\n"
                            "3. OVERALL ARC — one sentence (e.g. single peak at 1:05, "
                            "two peaks with breakdown between, etc.).\n\n"
                            "Be precise about dips and breakdowns — these are the most "
                            "important lighting moments. Give exact timestamps."
                        )},
                    ],
                }],
            )
            analysis = response.content[0].text.strip()
            self._track_analysis_cache[track_id] = analysis
            self._track_analysis = analysis
            self._analyzing_tids.discard(track_id)
            # Always clear "analyzing…" status; only update text if this deck's tab is active
            def _show_analysis(a=analysis, tid=track_id):
                self._analysis_status_lbl.configure(text="ready", text_color="green")
                tab = self._deck_tabview.get() if hasattr(self, '_deck_tabview') else None
                active_pnum = int(tab[1]) if (tab and len(tab) > 1) else None
                active_tid = self._deck_track_ids.get(active_pnum) if active_pnum else None
                if active_tid == tid or active_tid is None:
                    self._update_track_analysis_ui(a)
            self.after(0, _show_analysis)
        except Exception as e:
            self._analyzing_tids.discard(track_id)
            self.after(0, lambda err=str(e): self._analysis_status_lbl.configure(
                text=f"analysis error: {err[:40]}", text_color="red"))

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── API Key row ────────────────────────────────────────────────────
        key_frame = ctk.CTkFrame(self)
        key_frame.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkLabel(key_frame, text="Anthropic API Key:").pack(side="left", padx=5)
        self.key_entry = ctk.CTkEntry(key_frame, width=350, show="•")
        self.key_entry.pack(side="left", padx=5)
        saved_key = _load_ai_config().get("api_key", "")
        if saved_key:
            self.key_entry.insert(0, saved_key)

        self.init_btn = ctk.CTkButton(key_frame, text="Initialize AI",
                                      command=self._init_ai)
        self.init_btn.pack(side="left", padx=5)

        self.status_label = ctk.CTkLabel(key_frame, text="● Not initialized",
                                          text_color="gray")
        self.status_label.pack(side="left", padx=10)

        self._active_switch = ctk.CTkSwitch(
            key_frame, text="AI ON", width=20,
            onvalue=True, offvalue=False,
            command=self._on_active_toggle)
        self._active_switch.pack(side="left", padx=10)
        self._active_switch.select()
        if self._on_active_change:
            self._on_active_change(True)

        self._learn_btn = ctk.CTkButton(
            key_frame, text="Learn from Set", width=120, height=28,
            font=("", 11, "bold"), fg_color="#1a2a4a", hover_color="#2a4a7a",
            command=self._on_learn_btn)
        self._learn_btn.pack(side="right", padx=8)
        self._learn_lbl = ctk.CTkLabel(key_frame, text="", text_color="orange", font=("", 10))
        self._learn_lbl.pack(side="right", padx=4)

        # ── Per-deck plan tabs ─────────────────────────────────────────────
        ctk.CTkLabel(self, text="Track Plans",
                     font=("", 13, "bold")).pack(anchor="w", padx=14, pady=(4, 0))

        self._deck_tabview = ctk.CTkTabview(self, height=260,
                                             command=self._on_deck_tab_changed)
        self._deck_tabview.pack(fill="x", padx=10, pady=(0, 4))

        for n in range(1, 4):
            tab = self._deck_tabview.add(f"P{n}")
            self._build_deck_tab(n, tab)

        # ── Track analysis ─────────────────────────────────────────────────
        ta_frame = ctk.CTkFrame(self)
        ta_frame.pack(fill="x", padx=10, pady=(0, 4))

        ta_hdr = ctk.CTkFrame(ta_frame, fg_color="transparent")
        ta_hdr.pack(fill="x", padx=5, pady=(4, 0))
        ctk.CTkLabel(ta_hdr, text="Track Analysis",
                     font=("", 12, "bold")).pack(side="left")
        self._analysis_status_lbl = ctk.CTkLabel(ta_hdr, text="no track",
                                                   text_color="gray", font=("", 10))
        self._analysis_status_lbl.pack(side="left", padx=8)

        self._analysis_text = ctk.CTkTextbox(ta_frame, height=50, state="disabled",
                                              font=("", 10))
        self._analysis_text.pack(fill="x", padx=5, pady=(2, 4))

        # ── Event log ──────────────────────────────────────────────────────
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        ctk.CTkLabel(log_frame, text="AI Event Log",
                     font=("", 12, "bold")).pack(anchor="w", padx=5, pady=(4, 2))
        self.log_text = ctk.CTkTextbox(log_frame, height=120, font=("", 10))
        self.log_text.pack(fill="x", padx=5, pady=(0, 5))

    def _build_deck_tab(self, player_num, parent):
        """Build the per-deck plan view inside a tab frame."""
        # Status + regen row
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", pady=(2, 2))

        lbl = ctk.CTkLabel(hdr, text="no track", text_color="gray", font=("", 11))
        lbl.pack(side="left", padx=4)
        self._deck_plan_status_lbl[player_num] = lbl

        regen_btn = ctk.CTkButton(
            hdr, text="Regenerate", width=90, height=22,
            font=("", 10), fg_color="#444", hover_color="#666",
            command=lambda pn=player_num: self._on_regen_btn(pn))
        regen_btn.pack(side="right", padx=4)

        improve_btn = ctk.CTkButton(
            hdr, text="Improve", width=70, height=22,
            font=("", 10), fg_color="#1a4a1a", hover_color="#2a6a2a",
            command=lambda pn=player_num: self._on_improve_btn(pn))
        improve_btn.pack(side="right", padx=2)

        # Column header
        col_hdr = ctk.CTkFrame(parent, fg_color="transparent")
        col_hdr.pack(fill="x", padx=2, pady=(0, 1))
        for text, width in [("#", 28), ("Kind", 58), ("Effect", 140), ("Palette", 85),
                             ("Bright", 46), ("Speed", 42), ("Notes", 90)]:
            ctk.CTkLabel(col_hdr, text=text, width=width,
                         font=("", 9, "bold"), anchor="w").pack(side="left", padx=1)

        # "Now playing" position indicator
        now_lbl = ctk.CTkLabel(parent, text="NOW: —",
                               font=("", 10, "bold"), text_color="#888",
                               anchor="w")
        now_lbl.pack(fill="x", padx=4, pady=(0, 1))
        self._deck_now_playing_lbl[player_num] = now_lbl

        # Scrollable table
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=2)
        self._deck_plan_scroll[player_num] = scroll

    def regenerate_plan(self, track_id):
        """Clear plan for track_id so next plan_track call fires a fresh API request."""
        self._track_plans.pop(track_id, None)
        self._planning_tids.discard(track_id)
        self._deck_rendered = {k for k in self._deck_rendered if k[1] != track_id}
        self._log(f"Plan cleared for track {track_id} — will re-plan")

    # ── Internal helpers ───────────────────────────────────────────────────

    def _on_learn_btn(self):
        """End-of-set: analyse full session telemetry and extract general rules."""
        if not self._ai:
            self._log("AI not initialized")
            return
        if not self._session_data:
            self._log("No session data yet — play some tracks first")
            return
        self._learn_btn.configure(state="disabled")
        self._learn_lbl.configure(text="Analysing…", text_color="orange")
        threading.Thread(target=self._do_learn, daemon=True).start()

    def _do_learn(self):
        try:
            telemetry = self._build_session_telemetry()
            if not telemetry:
                raise ValueError("no telemetry data")
            result = self._ai.learn_from_session(telemetry)

            # Merge into existing learned rules
            prev_count = self._learned_rules.get("sessions_count", 0)
            self._learned_rules.update(result)
            self._learned_rules["sessions_count"] = prev_count + 1
            self._learned_rules["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

            # Apply brightness/speed floors immediately to light_ai post-processing
            if self._ai:
                self._ai.set_learned_floors(
                    result.get("brightness_floors", {}),
                    result.get("speed_floors", {}))
            _save_learned_rules(self._learned_rules)

            rules  = result.get("rules_text", "")
            style  = result.get("style_notes", "")
            obs    = result.get("session_observations", "")
            n_sess = self._learned_rules["sessions_count"]
            self._log(f"LEARNED  sessions={n_sess}  style: {style[:60]}")
            self._log(f"RULES: {rules[:120]}")
            if obs:
                self._log(f"OBS: {obs[:120]}")

            self.after(0, lambda: (
                self._learn_btn.configure(state="normal"),
                self._learn_lbl.configure(
                    text=f"✓ {n_sess} sessions learned", text_color="green"),
            ))
        except Exception as e:
            err = str(e)[:60]
            self._log(f"LEARN_FAILED  error={err}")
            self.after(0, lambda es=err: (
                self._learn_btn.configure(state="normal"),
                self._learn_lbl.configure(text=f"Failed: {es}", text_color="red"),
            ))

    def _on_improve_btn(self, player_num):
        """Improve plan for this deck using session data from this session."""
        if not self._ai:
            self._log("AI not initialized — can't improve")
            return
        tid = self._deck_track_ids.get(player_num)
        if not tid:
            self._log(f"P{player_num}: no track loaded")
            return
        session = self._session_data.get(tid)
        if not session or not session.get("beats"):
            self._log(f"P{player_num}: no session data yet — play the track first")
            return
        plan = self._track_plans.get(tid)
        if not plan:
            self._log(f"P{player_num}: no plan to improve")
            return
        if player_num in self._deck_plan_status_lbl:
            self._deck_plan_status_lbl[player_num].configure(
                text="Improving…", text_color="orange")
        threading.Thread(target=self._do_improve, args=(player_num, tid), daemon=True).start()

    def _do_improve(self, player_num, track_id):
        """Background: call AI to improve plan, save result, reload table."""
        try:
            plan     = self._track_plans.get(track_id, {})
            summary  = self._summarize_session(track_id)
            title    = self._track_titles.get(track_id, "")
            artist   = self._track_artists.get(track_id, "")
            bpm      = self._track_bpms.get(track_id, 0)
            phrases  = self._track_phrases.get(track_id, [])
            phrase_kinds = list(dict.fromkeys(p["kind"] for p in phrases)) if phrases else \
                           ["intro", "verse", "up", "chorus", "down", "bridge", "outro"]
            available_effects  = {k: self.effects.get_available_for_phrase(k) for k in phrase_kinds}
            available_palettes = {k: self.palettes.get_available_for_phrase(k) for k in phrase_kinds}

            original_list = [plan[k] for k in sorted(plan.keys())]
            improved_list = self._ai.improve_plan(
                original_plan=original_list, session_summary=summary,
                available_effects=available_effects, available_palettes=available_palettes,
                title=title, artist=artist, bpm=bpm,
            )
            if not improved_list:
                raise ValueError("empty plan returned")

            plan_sorted = sorted(improved_list, key=lambda e: e.get("phrase_index", 999))
            plan_dict   = {i: entry for i, entry in enumerate(plan_sorted)}
            self._track_plans[track_id] = plan_dict
            # Persist so it loads automatically next session
            self._improved_plans[str(track_id)] = plan_dict
            _save_improved_plans(self._improved_plans)
            self._deck_rendered.discard((player_num, track_id))
            n   = len(plan_dict)
            lbl = self._track_titles.get(track_id, f"track {track_id}")
            self._log(f"IMPROVED_PLAN_SAVED  track={track_id}  phrases={n}  title={lbl}")
            def _done(pn=player_num, tid=track_id, ph=phrases, pl=plan_dict, nn=n, lb=lbl):
                self._on_plan_ready(pn, tid, ph, pl, lb + " ★improved", nn)
            self.after(0, _done)
        except Exception as e:
            err = str(e)[:60]
            self._log(f"IMPROVE_FAILED  track={track_id}  error={err}")
            pn = player_num
            self.after(0, lambda es=err, pnum=pn: (
                self._deck_plan_status_lbl[pnum].configure(
                    text=f"Improve failed: {es}", text_color="red")
                if pnum in self._deck_plan_status_lbl else None))

    def _on_regen_btn(self, player_num):
        """Regenerate plan for the track currently loaded on this deck."""
        tid = self._deck_track_ids.get(player_num)
        if not tid:
            self._log(f"P{player_num}: no track loaded to regenerate")
            return
        self.regenerate_plan(tid)
        phrases  = self._track_phrases.get(tid)
        title    = self._track_titles.get(tid, "")
        artist   = self._track_artists.get(tid, "")
        bpm      = self._track_bpms.get(tid, 0)
        if phrases and self._ai:
            if player_num in self._deck_plan_status_lbl:
                self._deck_plan_status_lbl[player_num].configure(
                    text="Regenerating…", text_color="orange")
            self.plan_track(tid, phrases, title, artist, bpm, 0, player_num)

    def _on_active_toggle(self):
        active = self._active_switch.get()
        if active and not self._ai:
            self._init_ai()
            return
        if self._on_active_change:
            self._on_active_change(active)
        self._log("AI effects " + ("ON" if active else "OFF"))

    def _init_ai(self):
        api_key = self.key_entry.get().strip()
        if not api_key:
            self._log("Enter API key first")
            return
        try:
            from light_ai import LightAI
            self._ai = LightAI(api_key,
                               system_prompt_path=os.path.join(_HERE, "prompts", "track_plan.md"))
            # Apply any previously learned floors immediately
            if self._learned_rules:
                self._ai.set_learned_floors(
                    self._learned_rules.get("brightness_floors", {}),
                    self._learned_rules.get("speed_floors", {}))
            # Beta: globe planner
            try:
                from globe_planner import GlobePlanner
                self._globe_planner = GlobePlanner(self._ai.client, log_fn=self._log)
                self._log("Globe planner ready")
            except Exception as _ge:
                self._log(f"Globe planner unavailable: {_ge}")
            # Beta: vault strip planner
            try:
                from vault_planner import VaultPlanner
                self._vault_planner = VaultPlanner(self._ai.client, log_fn=self._log)
                self._log("Vault planner ready")
            except Exception as _ve:
                self._log(f"Vault planner unavailable: {_ve}")
            _save_ai_config({"api_key": api_key})
            self.status_label.configure(text="● Ready", text_color="green")
            self._log("AI initialized — waiting for track")
            self._active_switch.select()
            if self._on_active_change:
                self._on_active_change(True)
        except Exception as e:
            self.status_label.configure(text=f"● Error: {str(e)[:30]}", text_color="red")
            self._log(f"Init failed: {e}")

    def _on_deck_tab_changed(self):
        """Refresh the track analysis box when the user switches deck tabs."""
        tab = self._deck_tabview.get()   # e.g. "P1"
        try:
            pnum = int(tab[1])
        except (IndexError, ValueError):
            return
        tid = self._deck_track_ids.get(pnum)
        if not tid:
            return
        analysis = self._track_analysis_cache.get(tid)
        title = ""
        for _p, _t in self._deck_track_ids.items():
            if _t == tid:
                break
        # Get title from the plan label if available
        lbl_widget = self._deck_plan_status_lbl.get(pnum)
        if analysis:
            self._analysis_status_lbl.configure(text="ready", text_color="green")
            self._update_track_analysis_ui(analysis)
        else:
            # Clear text — analysis pending or not yet run
            self._analysis_status_lbl.configure(
                text=f"P{pnum} — no analysis yet", text_color="gray")
            self._analysis_text.configure(state="normal")
            self._analysis_text.delete("1.0", "end")
            self._analysis_text.configure(state="disabled")

    def _update_track_analysis_ui(self, analysis):
        self._analysis_status_lbl.configure(text="ready", text_color="green")
        self._analysis_text.configure(state="normal")
        self._analysis_text.delete("1.0", "end")
        self._analysis_text.insert("1.0", analysis)
        self._analysis_text.configure(state="disabled")

    def _render_deck_plan_table(self, player_num, track_id, phrases, plan):
        """Rebuild the plan table for a specific deck tab. Must be called on main thread."""
        if player_num is None or player_num not in self._deck_plan_scroll:
            return
        render_key = (player_num, track_id)
        if render_key in self._deck_rendered:
            return  # already rendered for this deck+track; highlight update handles the rest
        self._deck_rendered.add(render_key)
        scroll = self._deck_plan_scroll[player_num]
        rows   = self._deck_plan_rows[player_num]

        # Clear existing rows
        for w in scroll.winfo_children():
            w.destroy()
        rows.clear()

        cues = self._track_cues.get(track_id, [])
        track_bpm = self._track_bpms.get(track_id, 0)

        def _cue_ms(c):
            ms = c.get("time_ms", 0)
            if not ms:
                beat = c.get("beat", 0)
                if beat > 0 and track_bpm > 0:
                    ms = int(beat / track_bpm * 60 * 1000)
            return ms

        # ── Cue header block ──────────────────────────────────────────────────
        if cues:
            hdr = ctk.CTkFrame(scroll, fg_color="#1a1a2e", corner_radius=4)
            hdr.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(hdr, text="CUE POINTS", font=("", 9, "bold"),
                         text_color="#555", anchor="w").pack(side="left", padx=(6, 10))
            for c in cues:
                ms  = _cue_ms(c)
                mm, ss = divmod(ms // 1000, 60)
                tp  = c.get("type", "cue")
                lbl = c.get("label", "")
                nm  = c.get("name", "")
                tag = lbl if lbl else {"hot_cue": "HC", "loop": "LP", "memory": "M"}.get(tp, "C")
                txt = f"[{tag}] {mm}:{ss:02d}"
                if nm:
                    txt += f" {nm}"
                col = "#e8a030" if lbl else ("#44aaff" if tp == "loop" else "#888")
                ctk.CTkLabel(hdr, text=txt, font=("", 9), text_color=col,
                             anchor="w").pack(side="left", padx=(0, 8))

        for i, entry in enumerate(sorted(plan.values(), key=lambda e: e.get("phrase_index", 0))):
            idx = i  # use sorted position (0-based) as row key
            kind = entry.get("kind", "?")
            color = _PHRASE_COLORS.get(kind, "#aaa")

            row = ctk.CTkFrame(scroll, fg_color="transparent", corner_radius=4)
            row.pack(fill="x", pady=1)
            rows[idx] = row

            is_current = (player_num == self._current_phrase_player
                          and track_id == self._current_tid
                          and idx == self._current_phrase_idx)
            marker_text = f"▶{idx}" if is_current else f" {idx}"
            ctk.CTkLabel(row, text=marker_text, width=28,
                         font=("", 10, "bold" if is_current else "normal"),
                         text_color=color if is_current else "gray",
                         anchor="w").pack(side="left", padx=1)

            ctk.CTkLabel(row, text=kind, width=58, font=("", 10),
                         text_color=color, anchor="w").pack(side="left", padx=1)
            ctk.CTkLabel(row, text=entry.get("effect", "—"), width=140, font=("", 10),
                         anchor="w").pack(side="left", padx=1)
            ctk.CTkLabel(row, text=entry.get("palette", "—"), width=85, font=("", 10),
                         anchor="w").pack(side="left", padx=1)

            bright = entry.get("brightness")
            bright_s = f"{bright:.0%}" if bright is not None else "—"
            ctk.CTkLabel(row, text=bright_s, width=46, font=("", 10),
                         anchor="w").pack(side="left", padx=1)

            speed = entry.get("speed")
            speed_s = f"{speed:.1f}x" if speed is not None else "—"
            ctk.CTkLabel(row, text=speed_s, width=42, font=("", 10),
                         anchor="w").pack(side="left", padx=1)

            mpc = entry.get("mid_phrase_changes", [])
            notes = f"+{len(mpc)} mid" if mpc else ""
            ctk.CTkLabel(row, text=notes, width=90, font=("", 10),
                         text_color="gray", anchor="w").pack(side="left", padx=1)

            # Cue sub-rows: only show cues with valid beat data that fall within this phrase
            if cues and phrases and i < len(phrases):
                ph = phrases[i]
                b0 = ph.get("beat_start", 0)
                b1 = ph.get("beat_end", 999999)
                for c in cues:
                    cb = c.get("beat", 0)
                    if cb <= 0:
                        continue  # no beat data — skip (already shown in header)
                    if not (b0 <= cb < b1):
                        continue
                    ms  = _cue_ms(c)
                    mm, ss = divmod(ms // 1000, 60)
                    tp  = c.get("type", "cue")
                    lbl = c.get("label", "")
                    nm  = c.get("name", "")
                    tag = lbl if lbl else tp[0].upper()
                    beat_off = cb - b0
                    desc = f"  [{tag}] {mm}:{ss:02d}  +{beat_off}b"
                    if nm:
                        desc += f"  \"{nm}\""
                    cue_row = ctk.CTkFrame(scroll, fg_color="transparent", corner_radius=0)
                    cue_row.pack(fill="x", pady=0)
                    ctk.CTkLabel(cue_row, text=desc, font=("", 9), text_color="#888",
                                 anchor="w").pack(side="left", padx=(32, 2))

    def _update_deck_highlight(self, player_num, track_id, phrase_idx):
        """Update ▶ marker in the correct deck's plan table. Called on main thread."""
        if player_num is None:
            return

        rows = self._deck_plan_rows.get(player_num, {})

        # If track changed for this deck, re-render the table
        # Note: _deck_track_ids is now owned by notify_track_loaded — do NOT write it here
        if track_id != self._deck_track_ids.get(player_num):
            plan    = self._track_plans.get(track_id)
            phrases = self._track_phrases.get(track_id)
            if plan and phrases:
                self._render_deck_plan_table(player_num, track_id, phrases, plan)
                rows = self._deck_plan_rows[player_num]
                lbl  = self._track_titles.get(track_id, f"track {track_id}")
                if player_num in self._deck_plan_status_lbl:
                    self._deck_plan_status_lbl[player_num].configure(
                        text=f"Plan ready — {lbl[:30]} ({len(plan)} phrases)",
                        text_color="green")
            else:
                # No plan yet — clear stale table so old track's plan doesn't show
                scroll = self._deck_plan_scroll.get(player_num)
                if scroll:
                    for w in scroll.winfo_children():
                        w.destroy()
                    self._deck_plan_rows[player_num].clear()

        # Un-highlight old row for this deck (only if same track)
        if (self._current_phrase_player == player_num
                and self._current_tid == track_id
                and self._current_phrase_idx is not None):
            old_row = rows.get(self._current_phrase_idx)
            if old_row:
                old_row.configure(fg_color="transparent")
                children = old_row.winfo_children()
                if children:
                    children[0].configure(text=f" {self._current_phrase_idx}",
                                          font=("", 10, "normal"), text_color="gray")

        self._current_phrase_player = player_num
        self._current_tid = track_id
        self._current_phrase_idx = phrase_idx

        # Highlight new row
        new_row = rows.get(phrase_idx)
        plan = self._track_plans.get(track_id, {})
        entry = plan.get(phrase_idx, {})
        kind_str = entry.get("kind", "?")
        effect_str = entry.get("effect", "—")
        color = _PHRASE_COLORS.get(kind_str, "#aaa")
        if new_row:
            new_row.configure(fg_color="#2a3a2a")
            children = new_row.winfo_children()
            if children:
                children[0].configure(text=f"▶{phrase_idx}",
                                       font=("", 10, "bold"), text_color=color)
            # Scroll to make current row visible
            try:
                new_row.update_idletasks()
                scroll = self._deck_plan_scroll.get(player_num)
                if scroll:
                    total_h = scroll._scrollable_frame.winfo_height()
                    if total_h > 0:
                        scroll._parent_canvas.yview_moveto(
                            new_row.winfo_y() / total_h)
            except Exception:
                pass

        # Update "NOW" label
        now_lbl = self._deck_now_playing_lbl.get(player_num)
        if now_lbl:
            total_phrases = len(plan)
            now_text = (f"NOW: #{phrase_idx}  {kind_str}  {effect_str}"
                        + (f"  ({phrase_idx+1}/{total_phrases})" if total_phrases else ""))
            now_lbl.configure(text=now_text, text_color=color)

        # Auto-switch tab to this deck (only if not already there — avoids redraw flicker)
        try:
            target_tab = f"P{player_num}"
            if self._deck_tabview.get() != target_tab:
                self._deck_tabview.set(target_tab)
        except Exception:
            pass

    def _log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
