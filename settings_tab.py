"""
settings_tab.py — SETTINGS tab: edit settings.json from the app.

Live (no restart): phrase brightness, phrase palette pools, AI models.
Restart needed:    network ports, BLT URL, FPS.

Saving also writes the OSC/UDP ports into blt/vp_blt_code.clj, so BLT and the
app stay in sync. Reload the show in Beat Link Trigger after changing ports.
"""
import os
import re
import logging

import customtkinter as ctk

import settings

log = logging.getLogger("settings_tab")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BLT_CLJ = os.path.join(_HERE, "blt", "vp_blt_code.clj")

ACCENT = "#00cc88"
DIM = "#777777"
WARN = "#f5b830"
ERR = "#f53030"

AI_MODEL_CHOICES = [
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
]

_INT_FIELDS = [
    # key, label, min, max, help
    ("network.osc_port", "OSC port (BLT → app)", 1024, 65535, "beats, phrases, status"),
    ("network.udp_port", "UDP port (BLT → app)", 1024, 65535, "track metadata"),
    ("network.ddp_port", "DDP port (app → WLED)", 1, 65535, "WLED default is 4048"),
    ("network.feedback_port", "Feedback web port", 1024, 65535, "phone rating page"),
    ("render.fps", "Render FPS", 10, 120, "frames per second sent to the LEDs"),
]


class SettingsTab(ctk.CTkFrame):
    def __init__(self, master, palette_names_getter=None, on_phrase_settings_changed=None):
        super().__init__(master, fg_color="transparent")
        self._palette_names = palette_names_getter or (lambda: [])
        self._on_phrase_change = on_phrase_settings_changed
        self._int_entries = {}
        self._bright = {}
        self._bright_lbl = {}
        self._pools = {}
        self._build()
        self._load_into_widgets()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _section(self, parent, title, note=""):
        ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                     anchor="w").pack(fill="x", padx=12, pady=(16, 2))
        if note:
            ctk.CTkLabel(parent, text=note, text_color=DIM, anchor="w", justify="left",
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=12, pady=(0, 4))
        box = ctk.CTkFrame(parent)
        box.pack(fill="x", padx=12, pady=2)
        box.grid_columnconfigure(1, weight=1)
        return box

    def _build(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkButton(bar, text="Save", width=110, fg_color=ACCENT, text_color="#000",
                      command=self._save).pack(side="left")
        ctk.CTkButton(bar, text="Reset to defaults", width=140, fg_color="#333",
                      command=self._reset).pack(side="left", padx=8)
        self._status = ctk.CTkLabel(bar, text="", anchor="w")
        self._status.pack(side="left", padx=10, fill="x", expand=True)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(6, 0))

        # Network + render
        box = self._section(body, "Network & rendering",
                            "Restart djlights after changing these. OSC/UDP ports are also written "
                            "into blt/vp_blt_code.clj — reopen the show in Beat Link Trigger.")
        for row, (key, label, _lo, _hi, help_) in enumerate(_INT_FIELDS):
            ctk.CTkLabel(box, text=label, anchor="w", width=190).grid(row=row, column=0, padx=8, pady=3, sticky="w")
            e = ctk.CTkEntry(box, width=110)
            e.grid(row=row, column=1, padx=8, pady=3, sticky="w")
            ctk.CTkLabel(box, text=help_, text_color=DIM, anchor="w").grid(row=row, column=2, padx=8, sticky="w")
            self._int_entries[key] = e
        row = len(_INT_FIELDS)
        ctk.CTkLabel(box, text="BLT web server URL", anchor="w", width=190).grid(row=row, column=0, padx=8, pady=3, sticky="w")
        self._blt_url = ctk.CTkEntry(box, width=260)
        self._blt_url.grid(row=row, column=1, columnspan=2, padx=8, pady=3, sticky="w")

        # AI
        box = self._section(body, "AI models",
                            "Applied immediately. The API key is set in the AI CONTROL tab.")
        ctk.CTkLabel(box, text="Fast model", anchor="w", width=190).grid(row=0, column=0, padx=8, pady=3, sticky="w")
        self._fast_model = ctk.CTkComboBox(box, values=AI_MODEL_CHOICES, width=260)
        self._fast_model.grid(row=0, column=1, padx=8, pady=3, sticky="w")
        ctk.CTkLabel(box, text="live decisions + track plans", text_color=DIM).grid(row=0, column=2, padx=8, sticky="w")
        ctk.CTkLabel(box, text="Smart model", anchor="w", width=190).grid(row=1, column=0, padx=8, pady=3, sticky="w")
        self._smart_model = ctk.CTkComboBox(box, values=AI_MODEL_CHOICES, width=260)
        self._smart_model.grid(row=1, column=1, padx=8, pady=3, sticky="w")
        ctk.CTkLabel(box, text="learning after a session", text_color=DIM).grid(row=1, column=2, padx=8, sticky="w")

        # Phrases
        box = self._section(body, "Phrase defaults",
                            "Used when the AI hasn't planned the track (or AI is off). Applied immediately.\n"
                            "Palettes: comma-separated names from the palette editor; one is picked at random.")
        ctk.CTkLabel(box, text="Phrase", text_color=DIM).grid(row=0, column=0, padx=8, sticky="w")
        ctk.CTkLabel(box, text="Brightness", text_color=DIM).grid(row=0, column=1, padx=8, sticky="w")
        ctk.CTkLabel(box, text="Palettes", text_color=DIM).grid(row=0, column=3, padx=8, sticky="w")
        box.grid_columnconfigure(3, weight=1)
        for i, ph in enumerate(settings.PHRASES, start=1):
            ctk.CTkLabel(box, text=ph, anchor="w", width=70).grid(row=i, column=0, padx=8, pady=3, sticky="w")
            s = ctk.CTkSlider(box, from_=0, to=100, number_of_steps=100, width=160,
                              command=lambda v, p=ph: self._bright_lbl[p].configure(text=f"{int(v)}%"))
            s.grid(row=i, column=1, padx=8, pady=3, sticky="w")
            lbl = ctk.CTkLabel(box, text="", width=40)
            lbl.grid(row=i, column=2, padx=2, sticky="w")
            e = ctk.CTkEntry(box)
            e.grid(row=i, column=3, padx=8, pady=3, sticky="ew")
            self._bright[ph], self._bright_lbl[ph], self._pools[ph] = s, lbl, e

    # ── data ↔ widgets ────────────────────────────────────────────────────────
    def _load_into_widgets(self):
        for key, e in self._int_entries.items():
            e.delete(0, "end")
            e.insert(0, str(settings.get(key, "")))
        self._blt_url.delete(0, "end")
        self._blt_url.insert(0, settings.get("network.blt_url", ""))
        self._fast_model.set(settings.get("ai.fast_model", ""))
        self._smart_model.set(settings.get("ai.smart_model", ""))
        for ph in settings.PHRASES:
            v = float(settings.get(f"phrases.brightness.{ph}", 0.7)) * 100
            self._bright[ph].set(v)
            self._bright_lbl[ph].configure(text=f"{int(round(v))}%")
            self._pools[ph].delete(0, "end")
            self._pools[ph].insert(0, ", ".join(settings.get(f"phrases.palette_pools.{ph}", [])))

    def _save(self):
        before = settings.all_settings()
        errors = []
        for key, lo, hi, label in [(k, lo, hi, lbl) for k, lbl, lo, hi, _ in _INT_FIELDS]:
            raw = self._int_entries[key].get().strip()
            try:
                val = int(raw)
                if not lo <= val <= hi:
                    raise ValueError
                settings.set(key, val)
            except ValueError:
                errors.append(f"{label}: {lo}–{hi}")
        url = self._blt_url.get().strip()
        if not re.match(r"^https?://", url):
            errors.append("BLT URL must start with http://")
        else:
            settings.set("network.blt_url", url.rstrip("/"))
        if settings.get("network.osc_port") == settings.get("network.udp_port"):
            errors.append("OSC and UDP port must differ")
        if errors:
            # roll back anything partially set
            for k in ("network", "render"):
                settings.set(k, before[k])
            self._status.configure(text="Not saved — " + "; ".join(errors), text_color=ERR)
            return

        for key, w in (("ai.fast_model", self._fast_model), ("ai.smart_model", self._smart_model)):
            v = w.get().strip()
            if v:
                settings.set(key, v)

        known = set(self._palette_names() or [])
        unknown = []
        for ph in settings.PHRASES:
            settings.set(f"phrases.brightness.{ph}", round(self._bright[ph].get() / 100.0, 2))
            names = [n.strip() for n in self._pools[ph].get().split(",") if n.strip()]
            if names:
                settings.set(f"phrases.palette_pools.{ph}", names)
                if known:
                    unknown += [n for n in names if n not in known]

        settings.save()
        clj_msg = self._sync_blt_ports()
        if self._on_phrase_change:
            try:
                self._on_phrase_change()
            except Exception as e:
                log.warning("phrase settings callback failed: %s", e)

        after = settings.all_settings()
        restart = sorted(k for k in settings.RESTART_KEYS
                         if _dig(before, k) != _dig(after, k))
        msg, col = "Saved.", ACCENT
        if restart:
            msg += " Restart djlights to apply: " + ", ".join(restart) + "."
            col = WARN
        if clj_msg:
            msg += " " + clj_msg
        if unknown:
            msg += " Unknown palettes (ignored at runtime): " + ", ".join(sorted(set(unknown)))
            col = WARN
        self._status.configure(text=msg, text_color=col)

    def _reset(self):
        settings.reset_to_defaults()
        self._load_into_widgets()
        self._status.configure(text="Defaults loaded — press Save to keep them.", text_color=WARN)

    def _sync_blt_ports(self) -> str:
        """Write the OSC/UDP ports into blt/vp_blt_code.clj."""
        try:
            with open(_BLT_CLJ) as f:
                src = f.read()
        except OSError:
            return ""
        new = re.sub(r"\(def vp-osc-port \d+\)", f"(def vp-osc-port {settings.get('network.osc_port')})", src)
        new = re.sub(r"\(def vp-udp-port \d+\)", f"(def vp-udp-port {settings.get('network.udp_port')})", new)
        if new == src:
            return ""
        with open(_BLT_CLJ, "w") as f:
            f.write(new)
        return "BLT code updated — reopen the show in Beat Link Trigger."


def _dig(d, path):
    for p in path.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d
