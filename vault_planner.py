"""Vault strip plan manager — runs alongside the existing plan system.

Calls the AI with the vault-specific prompt and stores plans in a separate
dict.  Completely independent of the existing strip plan system.
"""

import json
import settings as _settings
import os
import threading
import time

_HERE             = os.path.dirname(os.path.abspath(__file__))
_VAULT_PLANS_FILE = os.path.join(_HERE, "vault_plans.json")


def _load_vault_plans():
    try:
        with open(_VAULT_PLANS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vault_plans(data):
    try:
        with open(_VAULT_PLANS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class VaultPlanner:
    """Plans vault-strip lighting shows.  Runs in background threads."""

    def __init__(self, ai_client, log_fn=None):
        """
        ai_client  — anthropic.Anthropic instance
        log_fn     — optional callable(str) for status messages
        """
        self._ai       = ai_client
        self._log      = log_fn or (lambda s: None)
        self._plans    = _load_vault_plans()   # track_id(str) → {phrase_index: entry}
        self._planning = set()                 # in-flight track_ids
        self._prompt   = self._load_prompt()

    def _load_prompt(self):
        path = os.path.join(_HERE, "prompts", "vault_gen_plan.md")
        try:
            with open(path) as f:
                return f.read()
        except Exception:
            return ""

    def has_plan(self, track_id) -> bool:
        return str(track_id) in self._plans

    def get_entry(self, track_id, phrase_idx: int) -> dict | None:
        plan = self._plans.get(str(track_id))
        if not plan:
            return None
        entry = plan.get(str(phrase_idx)) or plan.get(phrase_idx)
        if entry is not None:
            return entry
        # Overrun — return last entry
        if plan:
            keys = sorted(plan.keys(), key=lambda k: int(k) if str(k).isdigit() else 0)
            return plan[keys[-1]]
        return None

    def plan_async(self, track_id, phrases, title, artist, bpm, duration_ms,
                   cues=None, track_analysis=None, on_ready=None):
        """Fire a background plan request.  on_ready(track_id) called when done."""
        if not self._ai or not self._prompt:
            return
        tid_s = str(track_id)
        if tid_s in self._planning:
            return
        # Use saved plan if exists
        if tid_s in self._plans:
            self._log(f"VAULT_PLAN_CACHED  track={track_id}")
            if on_ready:
                on_ready(track_id)
            return
        self._planning.add(tid_s)

        def _run():
            try:
                plan_entries = self._call_ai(
                    phrases, title, artist, bpm, duration_ms, cues, track_analysis)
                if plan_entries:
                    plan_dict = {str(i): e for i, e in enumerate(
                        sorted(plan_entries, key=lambda e: e.get("phrase_index", 999)))}
                    self._plans[tid_s] = plan_dict
                    _save_vault_plans(self._plans)
                    self._log(f"VAULT_PLAN_READY  track={track_id}  "
                              f"phrases={len(plan_dict)}  title={title[:30]}")
                    if on_ready:
                        on_ready(track_id)
            except Exception as e:
                self._log(f"VAULT_PLAN_FAILED  track={track_id}  error={str(e)[:60]}")
            finally:
                self._planning.discard(tid_s)

        threading.Thread(target=_run, daemon=True,
                         name=f"vault-plan-{track_id}").start()

    def _call_ai(self, phrases, title, artist, bpm, duration_ms,
                 cues=None, track_analysis=None):
        dur_s   = duration_ms / 1000.0
        dur_str = (f"{int(dur_s // 60)}:{int(dur_s % 60):02d}"
                   if dur_s > 0 else "unknown")
        track_s = (f'"{title}" by {artist}'
                   if (title and artist) else (title or "unknown"))

        def _fmt_cue(c):
            ms = c.get("time_ms", 0)
            if not ms:
                beat = c.get("beat", 0)
                if beat > 0 and bpm > 0:
                    ms = int(beat / bpm * 60 * 1000)
            mm, ss = divmod(ms // 1000, 60)
            return dict(c, time=f"{mm}:{ss:02d}")

        input_json = {
            "track": {
                "title": track_s, "bpm": round(bpm, 1), "duration": dur_str,
                "track_analysis": track_analysis,
                "cues": [_fmt_cue(c) for c in (cues or [])],
            },
            "phrases": phrases,
            "device": {
                "type": "vault_strip",
                "strips": 5,
                "leds_per_strip": 300,
                "total_leds": 1500,
                "wiring": "snake",
            },
        }
        user_msg = (f"Plan the vault strip lighting show for this track:\n"
                    f"```json\n{json.dumps(input_json, indent=2)}\n```")

        resp = self._ai.messages.create(
            model=_settings.get("ai.fast_model", "claude-haiku-4-5"),
            max_tokens=4096,
            system=self._prompt,
            messages=[{"role": "user", "content": user_msg}],
            timeout=45.0,
        )
        text = resp.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        data = json.loads(text.strip())
        return data.get("vault_plan", [])

    def invalidate(self, track_id):
        """Force re-plan on next load."""
        self._plans.pop(str(track_id), None)
        _save_vault_plans(self._plans)
