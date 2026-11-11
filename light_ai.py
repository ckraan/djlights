import anthropic
import json
import settings as _settings
import logging

_log = logging.getLogger(__name__)


class LightAI:
    def __init__(self, api_key: str, system_prompt_path: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        with open(system_prompt_path) as f:
            self.system_prompt = f.read()

    def decide(self, context: dict) -> dict:
        """Ask AI what to do given current context.

        Args:
            context: dict with keys like phrase_type, beat_in_phrase, bpm,
                     current_effect, current_palette, energy_level, etc.

        Returns:
            dict with keys: action, effect, palette, brightness, speed, reason
        """
        user_message = f"""Current lighting state:
```json
{json.dumps(context, indent=2)}
```
What should the lights do next?"""

        try:
            response = self.client.messages.create(
                model=_settings.get("ai.fast_model", "claude-haiku-4-5"),
                max_tokens=500,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.AuthenticationError:
            _log.error("LightAI: invalid API key")
            return _fallback("invalid API key")
        except anthropic.RateLimitError:
            _log.warning("LightAI: rate limited")
            return _fallback("rate limited")
        except anthropic.APIConnectionError as e:
            _log.warning("LightAI: connection error: %s", e)
            return _fallback("connection error")
        except anthropic.APIStatusError as e:
            _log.warning("LightAI: API error %d: %s", e.status_code, e.message)
            return _fallback(f"API error {e.status_code}")

        text = response.content[0].text

        # Extract JSON from markdown code block if present
        if "```json" in text:
            try:
                text = text.split("```json")[1].split("```")[0]
            except IndexError:
                pass
        elif "```" in text:
            try:
                text = text.split("```")[1].split("```")[0]
            except IndexError:
                pass

        try:
            decision = json.loads(text.strip())
        except json.JSONDecodeError as e:
            _log.warning("LightAI: JSON parse failed: %s\nRaw: %r", e, text)
            return _fallback("JSON parse error")

        # Ensure all expected keys are present
        for key in ("action", "effect", "palette", "brightness", "speed",
                    "direction", "grouping", "spacing", "offset", "density",
                    "reason"):
            decision.setdefault(key, None)

        return decision

    def set_learned_floors(self, brightness_floors: dict, speed_floors: dict):
        """Apply brightness/speed floors learned from session data to post-processing."""
        self._learned_br_floors = brightness_floors or {}
        self._learned_sp_floors = speed_floors or {}

    def plan_track(self, phrases: list, available_effects: dict, available_palettes: dict,
                   title: str = "", artist: str = "", bpm: float = 128.0,
                   duration_ms: int = 0, track_analysis: str = None,
                   cues: list = None) -> list:
        """Plan lighting for an entire track in one API call.

        Args:
            phrases: list of dicts with keys: phrase_index, kind, beat_start, beat_end, duration_beats
            available_effects: dict kind → list of effect names
            available_palettes: dict kind → list of palette names
            title, artist, bpm, duration_ms: track metadata
            track_analysis: optional waveform analysis string from Claude vision

        Returns:
            list of plan entry dicts (one per phrase), each with effect/palette/brightness/etc.
            Raises on API error.
        """
        dur_s = duration_ms / 1000.0
        dur_str = f"{int(dur_s // 60)}:{int(dur_s % 60):02d}" if dur_s > 0 else "unknown"
        track_str = f'"{title}" by {artist}' if (title and artist) else (title or "unknown track")

        def _fmt_cue(c):
            ms = c.get("time_ms", 0)
            if not ms:
                beat = c.get("beat", 0)
                if beat > 0 and bpm > 0:
                    ms = int(beat / bpm * 60 * 1000)
            mm, ss = divmod(ms // 1000, 60)
            c2 = dict(c)
            c2["time"] = f"{mm}:{ss:02d}"
            return c2

        def _fmt_phrase(p):
            """Add MM:SS timestamps to each phrase for energy-map cross-referencing."""
            p2 = dict(p)
            if bpm > 0:
                t_start = p.get("beat_start", 0) / bpm * 60
                t_end   = p.get("beat_end",   0) / bpm * 60
                mm_s, ss_s = divmod(int(t_start), 60)
                mm_e, ss_e = divmod(int(t_end),   60)
                p2["time_start"] = f"{mm_s}:{ss_s:02d}"
                p2["time_end"]   = f"{mm_e}:{ss_e:02d}"
            return p2

        input_json = {
            "track": {
                "title": track_str,
                "bpm": round(bpm, 1),
                "duration": dur_str,
                "track_analysis": (
                    (track_analysis + "\n\nIMPORTANT: Each phrase below includes time_start/time_end "
                     "timestamps. Cross-reference these with the energy map above to set "
                     "brightness/speed/overlay per the actual energy at that moment — "
                     "override phrase-kind defaults where the energy disagrees.")
                    if track_analysis else None
                ),
                "cues": [_fmt_cue(c) for c in (cues or [])],
            },
            "phrases": [_fmt_phrase(p) for p in phrases],
            "available_effects": available_effects,
            "available_palettes": available_palettes,
        }

        # ── Chunked planning for long tracks ─────────────────────────────
        # Split into chunks of 50 phrases so token limits are never hit.
        CHUNK = 50
        all_phrases = input_json["phrases"]

        if len(all_phrases) <= CHUNK:
            chunks = [all_phrases]
        else:
            chunks = [all_phrases[i:i + CHUNK]
                      for i in range(0, len(all_phrases), CHUNK)]

        plan = []
        context_tail = []   # last 2 entries of previous chunk for continuity

        for chunk_idx, chunk_phrases in enumerate(chunks):
            chunk_input = dict(input_json)
            chunk_input["phrases"] = chunk_phrases
            if context_tail:
                chunk_input["previous_chunk_tail"] = context_tail

            _chunk_range = (
                f" (phrases {chunk_phrases[0]['phrase_index']}"
                f"–{chunk_phrases[-1]['phrase_index']} of {len(all_phrases)})"
                if len(chunks) > 1 else ""
            )
            user_message = (
                f"Plan the lighting show for this track{_chunk_range}:\n"
                f"```json\n{json.dumps(chunk_input, indent=2)}\n```"
            )

            response = self.client.messages.create(
                model=_settings.get("ai.fast_model", "claude-haiku-4-5"),
                max_tokens=8000,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_message}],
                timeout=60.0,
            )

            text = response.content[0].text.strip()
            if "```json" in text:
                try:
                    text = text.split("```json")[1].split("```")[0]
                except IndexError:
                    pass
            elif "```" in text:
                try:
                    text = text.split("```")[1].split("```")[0]
                except IndexError:
                    pass

            data = json.loads(text.strip())
            chunk_plan = data.get("plan", [])
            plan.extend(chunk_plan)
            context_tail = chunk_plan[-2:] if len(chunk_plan) >= 2 else chunk_plan

        # Ensure required keys on every entry
        for entry in plan:
            entry.setdefault("mid_phrase_changes", [])
            entry.setdefault("beat_overlay", "bar")
            entry.setdefault("render_mode", "full")
            entry.setdefault("direction", "forward")
            entry.setdefault("grouping", 1)
            entry.setdefault("spacing", 8)
            entry.setdefault("offset", 0.0)
            entry.setdefault("density", 1.0)
            entry.setdefault("active_strips", [2, 3, 4])
            entry.setdefault("beat_start", 0)
            entry.setdefault("beat_end", 0)

        # Post-process: enforce per-phrase-kind energy floors/ceilings.
        # AI (haiku) tends to generate conservative defaults; override them.
        _PHRASE_ENERGY = {
            # (brightness_min, speed_min, brightness_max, speed_max, strips_min)
            "intro":  (0.08, 0.10, 0.14, 0.25, 1),
            "verse":  (0.12, 0.15, 0.20, 0.35, 2),
            "up":     (0.16, 0.20, 0.22, 0.45, 3),
            "chorus": (0.18, 0.25, 0.25, 0.50, 4),
            "down":   (0.08, 0.10, 0.14, 0.25, 1),
            "bridge": (0.10, 0.12, 0.18, 0.30, 2),
            "outro":  (0.06, 0.10, 0.12, 0.22, 1),
        }
        # Merge learned floors on top of defaults
        learned_br = getattr(self, "_learned_br_floors", {})
        learned_sp = getattr(self, "_learned_sp_floors", {})
        _PHRASE_ENERGY_MERGED = {}
        for k, (br_mn, sp_mn, br_mx, sp_mx, str_mn) in _PHRASE_ENERGY.items():
            _PHRASE_ENERGY_MERGED[k] = (
                max(br_mn, learned_br.get(k, br_mn)),
                max(sp_mn, learned_sp.get(k, sp_mn)),
                br_mx, sp_mx, str_mn,
            )

        for entry in plan:
            kind = entry.get("kind", "verse")
            floors = _PHRASE_ENERGY_MERGED.get(kind)
            if not floors:
                continue
            br_min, sp_min, br_max, sp_max, str_min = floors
            br = entry.get("brightness")
            if br is not None:
                entry["brightness"] = max(br_min, min(br_max, br))
            sp = entry.get("speed")
            if sp is not None:
                entry["speed"] = max(sp_min, min(sp_max, sp))
            strips = entry.get("active_strips") or []
            if len(strips) < str_min:
                # Expand to use more strips — keep any existing + add more up to str_min
                all_s = [1, 2, 3, 4, 5]
                combined = sorted(set(strips) | set(all_s[:str_min]))
                entry["active_strips"] = combined
            # Cap spacing — enforce minimum dark space between groups
            _spacing_floor = {"chorus": 4, "up": 6, "verse": 8, "bridge": 10,
                              "intro": 14, "down": 8, "outro": 14}
            _spacing_cap   = {"chorus": 10, "up": 12, "verse": 16, "bridge": 18,
                              "intro": 22, "down": 16, "outro": 22}
            sp_floor = _spacing_floor.get(kind, 8)
            sp_cap   = _spacing_cap.get(kind, 16)
            entry["spacing"] = max(sp_floor, min(sp_cap, entry.get("spacing", sp_floor)))
            # Cap grouping — keep blocks tight and distinct
            _grouping_cap = {"chorus": 3, "up": 3, "verse": 2, "bridge": 2,
                             "intro": 2, "down": 2, "outro": 1}
            g_cap = _grouping_cap.get(kind, 2)
            if entry.get("grouping", 1) > g_cap:
                entry["grouping"] = g_cap

        return plan


    def improve_plan(self, original_plan: list, session_summary: list,
                     available_effects: dict, available_palettes: dict,
                     title: str = "", artist: str = "", bpm: float = 128.0,
                     improve_prompt_path: str = None) -> list:
        """Improve an existing plan based on real session data."""
        import os
        if improve_prompt_path is None:
            improve_prompt_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "prompts", "improve_plan.md")
        with open(improve_prompt_path) as f:
            system_prompt = f.read()

        track_str = f'"{title}" by {artist}' if (title and artist) else (title or "unknown track")
        input_json = {
            "track": {"title": track_str, "bpm": round(bpm, 1)},
            "original_plan": original_plan,
            "session_summary": session_summary,
            "available_effects": available_effects,
            "available_palettes": available_palettes,
        }
        user_message = (
            f"Improve the lighting plan for {track_str} based on session data:\n"
            f"```json\n{json.dumps(input_json, indent=2)}\n```"
        )
        response = self.client.messages.create(
            model=_settings.get("ai.fast_model", "claude-haiku-4-5"),
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout=45.0,
        )
        text = response.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        data = json.loads(text.strip())
        return data.get("plan", [])

    def learn_from_session(self, telemetry: dict, learn_prompt_path: str = None) -> dict:
        """Analyse session telemetry and return learned rules for future planning."""
        import os
        if learn_prompt_path is None:
            learn_prompt_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "prompts", "learn_from_session.md")
        with open(learn_prompt_path) as f:
            system_prompt = f.read()

        user_message = (
            "Analyse this DJ lighting session and extract general rules:\n"
            f"```json\n{json.dumps(telemetry, indent=2)}\n```"
        )
        response = self.client.messages.create(
            model=_settings.get("ai.smart_model", "claude-sonnet-4-6"),
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout=60.0,
        )
        text = response.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())


def _fallback(reason: str) -> dict:
    """Return a safe no-op decision when the API call fails."""
    return {
        "action":     "hold",
        "effect":     None,
        "palette":    None,
        "brightness": None,
        "speed":      None,
        "direction":  None,
        "grouping":   None,
        "spacing":    None,
        "offset":     None,
        "density":    None,
        "reason":     f"fallback: {reason}",
    }
