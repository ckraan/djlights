"""Vault Pattern Preview tab — browse all VaultRenderer patterns live in-app."""
import time
import tkinter as tk

import customtkinter as ctk
import numpy as np
from PIL import Image, ImageTk

from vault_renderer import VaultRenderer

# ── Constants ─────────────────────────────────────────────────────────────────

BPM    = 124.0
FPS    = 20
LED_W  = 4    # px per LED horizontally
LED_H  = 18   # px per LED vertically

PATTERNS = [
    ("matrix_rain",    "Digital rain — individual drops falling column by column"),
    ("sparks",         "Random individual LED sparks and embers"),
    ("ping_pong",      "Small blobs bouncing freely in 2D like billiard balls"),
    ("warp",           "Starfield — individual stars streaming outward from centre"),
    ("meteor_shower",  "Diagonal meteors with bright heads and decaying tails"),
    ("fireflies",      "Individual glowing points wandering on organic paths"),
    ("dna_helix",      "Two sine strands weaving with connecting rungs"),
    ("raindrops",      "Drops falling top→bottom, each column independent"),
    ("orbit",          "Bright points orbiting centres spaced along the canvas"),
    ("diagonal_cross", "Two thin diagonal beams crossing from opposite corners"),
    ("zigzag",         "Single bright zigzag line threading through all 5 rows"),
    ("spotlight",      "Hard spotlight moving on a figure-8 path"),
    ("corner_burst",   "Rings expanding from all 4 corners on every beat"),
    ("radial_rings",   "Sharp ring bands pulsing outward from canvas centre"),
    ("chevron",        "Thin V-shape arrowhead sweeping across the canvas"),
    ("plasma",         "Sine interference — only bright peaks visible"),
]

COLOR_PAIRS = [
    ([255,  30,   0], [  0,  10,  80]),   # fire / deep blue
    ([  0,  40, 255], [120,   0,   0]),   # electric blue / dark red
    ([255, 100,   0], [ 10,  10,  60]),   # amber / navy
    ([200,   0, 180], [  0,  80,  40]),   # magenta / dark green
    ([  0, 220, 180], [ 80,   0,  80]),   # cyan / purple
    ([255, 220,   0], [  0,   0, 100]),   # yellow / midnight blue
]

SPEED_PRESETS = [0.5, 1.0, 1.5, 2.5]
SPEED_LABELS  = ["0.5×", "1.0×", "1.5×", "2.5×"]

# Dark theme colours matching the app
BG     = "#0d0d0d"
BG2    = "#141414"
BG3    = "#1c1c1c"
BG4    = "#242424"
TEXT   = "#dddddd"
DIM    = "#555555"
ACCENT = "#1a6fff"
BORDER = "#2a2a2a"


# ── Tab widget ────────────────────────────────────────────────────────────────

class VaultPreviewTab(ctk.CTkFrame):
    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)

        self._renderer  = VaultRenderer()
        self._pat_idx   = 0
        self._col_idx   = 0
        self._spd_idx   = 1
        self._paused    = False
        self._t_start   = time.perf_counter()
        self._t_paused  = 0.0
        self._t_pause_at = 0.0
        self._tk_img    = None
        self._active    = False   # set True when tab is visible

        self._build()
        self._tick()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        cw = 300 * LED_W
        ch = 5   * LED_H

        # Canvas
        canvas_frame = ctk.CTkFrame(self, fg_color="#000000", corner_radius=0)
        canvas_frame.pack(fill="x", padx=12, pady=(12, 0))
        self._canvas = tk.Canvas(canvas_frame, width=cw, height=ch,
                                 bg="#000000", highlightthickness=0)
        self._canvas.pack()
        self._img_id = self._canvas.create_image(0, 0, anchor="nw")

        # Info row
        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(fill="x", padx=14, pady=(8, 0))

        self._name_lbl = ctk.CTkLabel(info, text="",
                                      font=ctk.CTkFont(size=14, weight="bold"),
                                      text_color=TEXT)
        self._name_lbl.pack(anchor="w")

        self._desc_lbl = ctk.CTkLabel(info, text="",
                                      font=ctk.CTkFont(size=11),
                                      text_color=DIM)
        self._desc_lbl.pack(anchor="w")

        self._count_lbl = ctk.CTkLabel(info, text="",
                                       font=ctk.CTkFont(size=9),
                                       text_color="#333333")
        self._count_lbl.pack(anchor="w")

        # Controls
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(pady=(10, 12))

        btn = dict(height=28, corner_radius=4, font=ctk.CTkFont(size=10, weight="bold"),
                   fg_color=BG4, hover_color=BG3, text_color=TEXT,
                   border_color=BORDER, border_width=1)

        ctk.CTkButton(ctrl, text="◀  Prev",  width=80, command=self._prev, **btn
                      ).grid(row=0, column=0, padx=4)
        ctk.CTkButton(ctrl, text="Next  ▶",  width=80, command=self._next, **btn
                      ).grid(row=0, column=1, padx=4)

        self._pause_btn = ctk.CTkButton(ctrl, text="⏸  Pause", width=90,
                                        command=self._toggle_pause, **btn)
        self._pause_btn.grid(row=0, column=2, padx=4)

        self._spd_btn = ctk.CTkButton(ctrl,
                                      text=f"Speed {SPEED_LABELS[self._spd_idx]}",
                                      width=90, command=self._cycle_speed, **btn)
        self._spd_btn.grid(row=0, column=3, padx=4)

        self._col_btn = ctk.CTkButton(ctrl, text="Colors ●", width=80,
                                      command=self._cycle_colors, **btn)
        self._col_btn.grid(row=0, column=4, padx=4)

        # Pattern list
        list_frame = ctk.CTkFrame(self, fg_color=BG2, corner_radius=6)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        ctk.CTkLabel(list_frame, text="PATTERNS",
                     font=ctk.CTkFont(size=9, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", padx=10, pady=(6, 2))

        scroll = ctk.CTkScrollableFrame(list_frame, fg_color="transparent",
                                        scrollbar_button_color=BG4)
        scroll.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self._pat_btns = []
        for i, (name, desc) in enumerate(PATTERNS):
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=1)
            idx = i
            lbl = ctk.CTkLabel(row, text=f"{i+1:02d}  {name}",
                               font=ctk.CTkFont(size=10, weight="bold"),
                               text_color=TEXT, anchor="w", cursor="hand2")
            lbl.pack(side="left", padx=(4, 6))
            lbl.bind("<Button-1>", lambda _e, n=idx: self._jump(n))
            sub = ctk.CTkLabel(row, text=desc,
                               font=ctk.CTkFont(size=9),
                               text_color=DIM, anchor="w")
            sub.pack(side="left")
            sub.bind("<Button-1>", lambda _e, n=idx: self._jump(n))
            self._pat_btns.append(lbl)

        self._update_labels()

    # ── Controls ──────────────────────────────────────────────────────────────

    def _prev(self):
        self._pat_idx = (self._pat_idx - 1) % len(PATTERNS)
        self._update_labels()

    def _next(self):
        self._pat_idx = (self._pat_idx + 1) % len(PATTERNS)
        self._update_labels()

    def _jump(self, idx):
        self._pat_idx = idx
        self._update_labels()

    def _toggle_pause(self):
        if self._paused:
            self._t_paused += time.perf_counter() - self._t_pause_at
            self._paused = False
            self._pause_btn.configure(text="⏸  Pause")
        else:
            self._t_pause_at = time.perf_counter()
            self._paused = True
            self._pause_btn.configure(text="▶  Resume")

    def _cycle_speed(self):
        self._spd_idx = (self._spd_idx + 1) % len(SPEED_PRESETS)
        self._spd_btn.configure(text=f"Speed {SPEED_LABELS[self._spd_idx]}")

    def _cycle_colors(self):
        self._col_idx = (self._col_idx + 1) % len(COLOR_PAIRS)
        c1 = COLOR_PAIRS[self._col_idx][0]
        self._col_btn.configure(text_color=f"#{c1[0]:02x}{c1[1]:02x}{c1[2]:02x}")

    def _update_labels(self):
        name, desc = PATTERNS[self._pat_idx]
        self._name_lbl.configure(text=name)
        self._desc_lbl.configure(text=desc)
        self._count_lbl.configure(
            text=f"{self._pat_idx + 1} / {len(PATTERNS)}  —  click list or ← → to browse")
        # Highlight active row
        for i, lbl in enumerate(self._pat_btns):
            lbl.configure(text_color=ACCENT if i == self._pat_idx else TEXT)

    # ── Render loop ───────────────────────────────────────────────────────────

    def set_active(self, active: bool):
        """Called by the main app when this tab becomes visible/hidden."""
        self._active = active

    def _tick(self):
        if self._active and not self._paused:
            t = time.perf_counter() - self._t_start - self._t_paused
            beat_phase = (t * BPM / 60.0) % 1.0
            name, _ = PATTERNS[self._pat_idx]
            c1, c2  = COLOR_PAIRS[self._col_idx]
            speed   = SPEED_PRESETS[self._spd_idx]

            params = {
                "pattern":         name,
                "color_primary":   c1,
                "color_secondary": c2,
                "speed":           speed,
                "brightness":      0.92,
                "coverage":        0.90,
                "size":            0.22,
                "beat_sync":       "beat",
            }
            try:
                px = self._renderer.render(t, BPM, params, beat_phase)
                grid = px.reshape(5, 300, 3)
                img_arr = np.repeat(np.repeat(grid, LED_H, axis=0), LED_W, axis=1)
                img = Image.fromarray(img_arr.astype(np.uint8), mode="RGB")
                self._tk_img = ImageTk.PhotoImage(img)
                self._canvas.itemconfig(self._img_id, image=self._tk_img)
            except Exception:
                pass

        self.after(1000 // FPS, self._tick)
