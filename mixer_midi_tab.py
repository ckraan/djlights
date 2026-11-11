"""
mixer_midi_tab.py — MIDI Mixer tab for djlights.

Displays DJM-A9 fader/EQ state and provides deck weight readout
used by the deck selection logic.

Self-contained: no imports from wled_ctrl or blt_client.
"""

import customtkinter as ctk
import threading

BG   = "#0a0a0a"
BG2  = "#111111"
BG3  = "#1a1a1a"
BG4  = "#222222"
ACCENT  = "#00cc88"
ACCENT2 = "#0077dd"
TEXT    = "#ffffff"
DIM     = "#777777"
BORDER  = "#2a2a2a"
WARN    = "#cc8800"
KILL    = "#cc2233"


def _bar_color(val: float) -> str:
    """Green → yellow → red based on 0–1 value."""
    if val > 0.7:
        return ACCENT
    if val > 0.3:
        return ACCENT2
    return KILL


class MixerMidiTab(ctk.CTkFrame):
    """
    Tab content for MIDI mixer monitoring and deck selection.
    Pass a MixerMidi instance (can be disconnected at init time).
    """

    def __init__(self, master, midi: "MixerMidi"):
        super().__init__(master, fg_color="transparent")
        self._midi = midi
        self._polling = False
        self._build_ui()
        self._start_poll()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Connection row ────────────────────────────────────────────────────
        conn_frame = ctk.CTkFrame(self, fg_color=BG3, corner_radius=6)
        conn_frame.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(conn_frame, text="DJM-A9 MIDI",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=ACCENT).pack(side="left", padx=10, pady=8)

        self._status_lbl = ctk.CTkLabel(conn_frame, text="● Disconnected",
                                         font=ctk.CTkFont(size=11),
                                         text_color=KILL)
        self._status_lbl.pack(side="left", padx=10)

        self._port_var = ctk.StringVar(value="auto-detect")
        self._port_menu = ctk.CTkOptionMenu(conn_frame,
                                             variable=self._port_var,
                                             values=["auto-detect"],
                                             width=260,
                                             font=ctk.CTkFont(size=11))
        self._port_menu.pack(side="left", padx=8)

        self._refresh_btn = ctk.CTkButton(conn_frame, text="Refresh Ports",
                                           width=110, height=28,
                                           font=ctk.CTkFont(size=10),
                                           fg_color=BG4, hover_color=BG3,
                                           border_color=BORDER, border_width=1,
                                           text_color=DIM,
                                           command=self._refresh_ports)
        self._refresh_btn.pack(side="left", padx=4)

        self._connect_btn = ctk.CTkButton(conn_frame, text="Connect",
                                           width=90, height=28,
                                           font=ctk.CTkFont(size=10, weight="bold"),
                                           fg_color=ACCENT2, hover_color="#005faa",
                                           text_color=TEXT,
                                           command=self._connect)
        self._connect_btn.pack(side="left", padx=8)

        self._ch_var = ctk.StringVar(value="Ch 1")
        ctk.CTkOptionMenu(conn_frame,
                          variable=self._ch_var,
                          values=[f"Ch {i}" for i in range(1, 17)],
                          width=70,
                          font=ctk.CTkFont(size=10)).pack(side="right", padx=8)
        ctk.CTkLabel(conn_frame, text="MIDI Ch:",
                     font=ctk.CTkFont(size=10),
                     text_color=DIM).pack(side="right", padx=(8, 0))

        # ── Channel strips ────────────────────────────────────────────────────
        strips_frame = ctk.CTkFrame(self, fg_color="transparent")
        strips_frame.pack(fill="both", expand=True, padx=12, pady=6)

        self._strips = []
        for ch in range(1, 5):
            strip = _ChannelStrip(strips_frame, ch)
            strip.pack(side="left", fill="both", expand=True, padx=4)
            self._strips.append(strip)

        # ── Deck weight readout ───────────────────────────────────────────────
        weight_frame = ctk.CTkFrame(self, fg_color=BG3, corner_radius=6)
        weight_frame.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkLabel(weight_frame, text="Deck weights →",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=DIM).pack(side="left", padx=10, pady=6)

        self._weight_lbls = {}
        for ch in range(1, 5):
            ctk.CTkLabel(weight_frame,
                         text=f"P{ch}:", font=ctk.CTkFont(size=11),
                         text_color=DIM).pack(side="left", padx=(8, 2))
            lbl = ctk.CTkLabel(weight_frame, text="—",
                               font=ctk.CTkFont(size=11, weight="bold"),
                               text_color=DIM, width=44)
            lbl.pack(side="left", padx=(0, 6))
            self._weight_lbls[ch] = lbl

        self._active_lbl = ctk.CTkLabel(weight_frame, text="Active: —",
                                         font=ctk.CTkFont(size=12, weight="bold"),
                                         text_color=ACCENT)
        self._active_lbl.pack(side="right", padx=16)

        # ── Info note ─────────────────────────────────────────────────────────
        ctk.CTkLabel(self,
                     text="Bass cut threshold: LOW EQ < 44/127  ·  Fader 0 = closed  ·  127 = open",
                     font=ctk.CTkFont(size=10), text_color=DIM
                     ).pack(pady=(0, 8))

        self._refresh_ports()

    # ── Port management ───────────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = self._midi.available_ports()
        options = ["auto-detect"] + ports
        self._port_menu.configure(values=options)
        if not self._port_var.get() in options:
            self._port_var.set("auto-detect")

    def _connect(self):
        sel = self._port_var.get()
        ch  = int(self._ch_var.get().split()[1]) - 1  # 0-indexed

        if self._midi.is_connected:
            self._midi.stop()

        self._midi.midi_channel = ch
        port = None if sel == "auto-detect" else sel

        def _try():
            ok = self._midi.start(port)
            self.after(0, lambda: self._on_connect_result(ok))

        threading.Thread(target=_try, daemon=True).start()
        self._connect_btn.configure(state="disabled", text="Connecting…")

    def _on_connect_result(self, ok: bool):
        self._connect_btn.configure(state="normal", text="Connect")
        if ok:
            self._status_lbl.configure(
                text=f"● {self._midi.port_name}", text_color=ACCENT)
        else:
            self._status_lbl.configure(text="● Failed", text_color=KILL)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _start_poll(self):
        self._polling = True
        self._poll()

    def _poll(self):
        if not self._polling:
            return
        try:
            self._update_display()
        except Exception:
            pass
        self.after(100, self._poll)   # 10 fps is plenty for knobs

    def _update_display(self):
        raw     = self._midi.get_raw()
        weights = self._midi.get_deck_weights()

        # Update channel strips
        for i, strip in enumerate(self._strips):
            ch = i + 1
            strip.update(raw[ch])

        # Update weight row
        best_ch  = max(weights, key=lambda c: weights[c])
        best_val = weights[best_ch]

        for ch, lbl in self._weight_lbls.items():
            w = weights[ch]
            pct = f"{w:.0%}"
            lbl.configure(text=pct,
                          text_color=ACCENT if ch == best_ch and best_val > 0.05
                          else DIM)

        if best_val > 0.05:
            self._active_lbl.configure(
                text=f"Active: P{best_ch}  ({best_val:.0%})",
                text_color=ACCENT)
        else:
            self._active_lbl.configure(text="Active: —", text_color=DIM)

        # Connection status heartbeat
        age = self._midi.seconds_since_last_msg
        if self._midi.is_connected:
            if age < 5:
                self._status_lbl.configure(
                    text=f"● {self._midi.port_name}", text_color=ACCENT)
            else:
                self._status_lbl.configure(
                    text=f"● {self._midi.port_name}  (idle)", text_color=WARN)


class _ChannelStrip(ctk.CTkFrame):
    """Visual strip for one mixer channel: fader bar + EQ knob values."""

    _KNOB_LABELS = ("HI", "MID", "LOW", "TRIM")
    _KNOB_KEYS   = ("hi", "mid", "low", "trim")

    def __init__(self, master, ch: int):
        super().__init__(master, fg_color=BG2, corner_radius=8, border_width=1,
                         border_color=BORDER)
        self._ch = ch
        self._build()

    def _build(self):
        # Channel label
        ctk.CTkLabel(self, text=f"CH {self._ch}",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT).pack(pady=(8, 4))

        # Fader bar
        ctk.CTkLabel(self, text="FADER", font=ctk.CTkFont(size=9),
                     text_color=DIM).pack()
        self._fader_bar = ctk.CTkProgressBar(self, width=14, height=120,
                                              orientation="vertical",
                                              progress_color=ACCENT,
                                              fg_color=BG4)
        self._fader_bar.pack(pady=(2, 8))
        self._fader_bar.set(1.0)

        self._fader_lbl = ctk.CTkLabel(self, text="127",
                                        font=ctk.CTkFont(size=9),
                                        text_color=DIM)
        self._fader_lbl.pack()

        ctk.CTkFrame(self, height=1, fg_color=BORDER).pack(fill="x",
                                                            padx=6, pady=6)

        # EQ knobs (displayed as value + colored dot)
        self._eq_lbls = {}
        for key, label in zip(self._KNOB_KEYS, self._KNOB_LABELS):
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)
            ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=9),
                         text_color=DIM, width=28).pack(side="left")
            val_lbl = ctk.CTkLabel(row, text="64",
                                   font=ctk.CTkFont(size=9, weight="bold"),
                                   text_color=DIM, width=28)
            val_lbl.pack(side="left")
            self._eq_lbls[key] = val_lbl

        # Bass status badge
        self._bass_badge = ctk.CTkLabel(self, text="BASS OK",
                                         font=ctk.CTkFont(size=9, weight="bold"),
                                         text_color=ACCENT,
                                         fg_color=BG3, corner_radius=4)
        self._bass_badge.pack(pady=(4, 8))

    def update(self, data: dict):
        fader = data["fader"]
        self._fader_bar.set(fader / 127.0)
        self._fader_bar.configure(progress_color=_bar_color(fader / 127.0))
        self._fader_lbl.configure(text=str(fader))

        for key in self._KNOB_KEYS:
            val = data[key]
            delta = val - 64   # -64 to +63
            sign  = "+" if delta >= 0 else ""
            color = ACCENT if abs(delta) < 10 else (WARN if abs(delta) < 30 else KILL)
            self._eq_lbls[key].configure(
                text=f"{sign}{delta}", text_color=color)

        # Bass badge
        bass_cut = data["low"] < (64 - 20)
        if bass_cut:
            self._bass_badge.configure(text="BASS CUT", text_color=KILL,
                                        fg_color="#330008")
        else:
            self._bass_badge.configure(text="BASS OK", text_color=ACCENT,
                                        fg_color=BG3)
