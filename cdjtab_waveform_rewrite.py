"""
CDJTab waveform rendering rewrite
==================================
Drop-in replacements for the methods in CDJTab (wled_ctrl.py).

HOW TO APPLY
------------
1. In CDJTab.__init__, add after self._last_cue_key / self._retry_times:

    self._assigned = [None, None]   # committed best/second player objects
    self._cand     = [None, None]   # candidate player (pending confirmation)
    self._cand_n   = [0,    0]      # consecutive frames same candidate seen

2. Replace the ranked/best/second block in CDJTab.update() with the new
   _pick_players() call (see below).

3. Replace _update_timeline(), _draw_waveform(), _draw_timeline(),
   _draw_time_ruler() with the versions below.

All other methods (build, _update_cues, etc.) stay unchanged.
"""

# ─────────────────────────────────────────────────────────────────────────────
# PASTE into CDJTab.__init__  (after self._retry_times = {} line)
# ─────────────────────────────────────────────────────────────────────────────
#
#   self._assigned = [None, None]
#   self._cand     = [None, None]
#   self._cand_n   = [0,    0]
#

# ─────────────────────────────────────────────────────────────────────────────
# REPLACE the ranked/best/second block + the two _update_timeline calls
# inside CDJTab.update()  with:
# ─────────────────────────────────────────────────────────────────────────────
#
#   best, second = self._pick_players(cdjs)
#
#   for i in range(1, 5):
#       self._cards[i].update(clients.get(i))
#
#   for tl, client in zip(self._tl, [best, second]):
#       self._update_timeline(tl, client, prodj)
#
#   self._update_cues(best, prodj)
#

# ─────────────────────────────────────────────────────────────────────────────
# NEW METHOD — paste anywhere inside CDJTab class
# ─────────────────────────────────────────────────────────────────────────────

def _pick_players(self, cdjs):
    """Return (best, second) with 8-frame hysteresis to avoid flickering."""
    HYSTERESIS = 8

    def _score(c):
        bpm_ok = isinstance(c.bpm, (int, float)) and c.bpm > 0
        return sum([
            getattr(c, "on_air", False),
            c.play_state in ("playing", "cueing"),
            bpm_ok,
        ])

    ranked = sorted(cdjs, key=_score, reverse=True)

    result = []
    for slot in range(2):
        new_cand = ranked[slot] if len(ranked) > slot else None
        if new_cand is self._cand[slot]:
            self._cand_n[slot] += 1
        else:
            self._cand[slot]   = new_cand
            self._cand_n[slot] = 1
        if self._cand_n[slot] >= HYSTERESIS:
            self._assigned[slot] = new_cand
        # If no committed assignment yet, use candidate directly (startup)
        result.append(self._assigned[slot] if self._assigned[slot] is not None else new_cand)

    return result[0], result[1]


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE _update_timeline entirely
# ─────────────────────────────────────────────────────────────────────────────

def _update_timeline(self, tl, client, prodj):
    if client is None:
        tl["player_lbl"].configure(text="—", text_color=DIM)
        tl["phrase_lbl"].configure(text="", text_color=DIM)
        tl["time_lbl"].configure(text="—:— / —:—", text_color=DIM)
        for cv_key in ("waveform_cv", "phrase_cv", "time_cv"):
            tl[cv_key].delete("all")
        return

    pn    = client.player_number
    meta  = client.metadata
    pssi  = getattr(client, "pssi", None)
    pos_s = client.position
    dur_s = None
    if meta:
        try:    dur_s = float(str(meta.get("duration", 0) or 0)) or None
        except: pass

    is_playing = client.play_state in ("playing", "cueing")
    on_air     = getattr(client, "on_air", False)
    p_col = "#00ff88" if client.status_packet_received else (
            KILL_COL if on_air else (ACCENT if is_playing else DIM))
    tl["player_lbl"].configure(text=f"P{pn}", text_color=p_col)

    phrase = _phrase_for_client(client)
    tl["phrase_lbl"].configure(text=phrase.upper() if phrase else "",
                                text_color=PHRASE_COLORS.get(phrase, DIM))

    if pos_s is not None and dur_s:
        pm, ps = divmod(int(max(0, pos_s)), 60)
        dm, ds = divmod(int(dur_s), 60)
        tl["time_lbl"].configure(text=f"{pm}:{ps:02d} / {dm}:{ds:02d}", text_color=TEXT)
    elif pos_s is not None:
        pm, ps = divmod(int(max(0, pos_s)), 60)
        tl["time_lbl"].configure(text=f"{pm}:{ps:02d}", text_color=DIM)
    else:
        tl["time_lbl"].configure(text="—:— / —:—", text_color=DIM)

    # Fetch waveform (prefer color PWV5, fall back to plain PWAV)
    wvf_key = (client.loaded_player_number or pn, client.loaded_slot or "usb", client.track_id)
    waveform = None
    try:    waveform = prodj.data.color_waveform_store[wvf_key]
    except KeyError: pass
    if waveform is None:
        try:    waveform = prodj.data.waveform_store[wvf_key]
        except KeyError: pass

    # Fetch cues for waveform markers
    cues = []
    raw = getattr(client, "cue_extended", None)
    if raw is None:
        try:    raw = prodj.data.cue_extended_store[wvf_key]
        except KeyError: raw = None
    if raw:
        cues = [c for c in raw if c.get("hot_cue", 0) > 0]

    self._draw_waveform(waveform, dur_s, pos_s, tl["waveform_cv"],
                        phrases=pssi, cues=cues)
    self._draw_timeline(client, pssi, dur_s, tl["phrase_cv"])
    self._draw_time_ruler(dur_s, pos_s, tl["time_cv"])


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE _draw_waveform entirely
# ─────────────────────────────────────────────────────────────────────────────

def _draw_waveform(self, waveform, dur_s, pos_s, cv, phrases=None, cues=None):
    """PIL/numpy scrolling viewport — single tk image call per frame."""
    import numpy as np
    from PIL import Image, ImageTk

    w = cv.winfo_width()
    h = cv.winfo_height()
    if w < 4 or h < 4:
        return

    VIEW_S    = 30.0    # seconds visible in window
    HEAD_FRAC = 0.33    # playhead position as fraction from left edge

    if pos_s is None:
        pos_s = 0.0

    view_start = pos_s - HEAD_FRAC * VIEW_S          # leftmost time shown
    head_x     = int(HEAD_FRAC * w)

    # ── Build pixel array ────────────────────────────────────────────────────
    arr = np.full((h, w, 3), [6, 6, 14], dtype=np.uint8)  # dark background
    mid = h // 2
    scale = mid / 32.0

    if waveform and len(waveform) > 0:
        n_cols   = len(waveform)
        is_color = not isinstance(waveform, (bytes, bytearray))

        # Build a time-to-column lookup for speed
        for px_x in range(w):
            t = view_start + (px_x / w) * VIEW_S
            if t < 0.0:
                continue
            if dur_s and t > dur_s:
                continue

            # Map time → waveform column
            if dur_s and dur_s > 0.0:
                wvf_idx = int(t / dur_s * n_cols)
            else:
                wvf_idx = int(px_x / w * n_cols)
            wvf_idx = max(0, min(n_cols - 1, wvf_idx))

            v = waveform[wvf_idx]
            if is_color:
                # PWV5 format
                ht = (v >> 2)  & 0x1F
                r  = ((v >> 13) & 0x07) * 36
                g  = ((v >> 10) & 0x07) * 36
                b  = ((v >>  7) & 0x07) * 36
            else:
                # Plain PWAV format
                ht    = (v >> 3) & 0x1F
                white = (v & 0x07) * 28
                r     = white // 3
                g     = white // 2
                b     = 80 + white

            px_h = max(2, int(ht * scale))
            y0   = max(0, mid - px_h)
            y1   = min(h, mid + px_h)
            arr[y0:y1, px_x] = [r, g, b]

    else:
        # No waveform — dim placeholder lines
        arr[mid - 2:mid + 2, ::2] = [26, 26, 42]

    # ── Phrase band — bottom 5 rows ──────────────────────────────────────────
    if phrases and dur_s and dur_s > 0:
        entries  = phrases.get("entries", [])
        end_beat = (phrases.get("end_beat", 1) or 1) - 1
        if entries and end_beat > 0:
            for idx, e in enumerate(entries):
                sb = e["beat"] - 1
                eb = (entries[idx + 1]["beat"] - 1) if idx + 1 < len(entries) else end_beat
                t_start = sb / end_beat * dur_s
                t_end   = eb / end_beat * dur_s
                x1 = int((t_start - view_start) / VIEW_S * w)
                x2 = int((t_end   - view_start) / VIEW_S * w)
                x1, x2 = max(0, x1), min(w, x2)
                if x2 > x1:
                    ch = PHRASE_COLORS.get(e["kind_name"], "#333333")
                    rgb = (int(ch[1:3], 16), int(ch[3:5], 16), int(ch[5:7], 16))
                    arr[h - 5:h, x1:x2] = rgb

    # ── Hot cue vertical markers ─────────────────────────────────────────────
    if cues and dur_s and dur_s > 0:
        for cue in cues:
            t_cue = cue.get("time_ms", 0) / 1000.0
            cx    = int((t_cue - view_start) / VIEW_S * w)
            if 0 <= cx < w:
                cr = cue.get("color_r", 200)
                cg = cue.get("color_g", 200)
                cb = cue.get("color_b", 200)
                # 2px wide marker
                x0 = max(0, cx - 1)
                x1 = min(w, cx + 1)
                arr[:h - 5, x0:x1] = [cr, cg, cb]

    # ── Playhead (white vertical line at HEAD_FRAC) ──────────────────────────
    hx0 = max(0, head_x - 1)
    hx1 = min(w, head_x + 1)
    arr[:, hx0:hx1] = [255, 255, 255]

    # ── Render ───────────────────────────────────────────────────────────────
    photo = ImageTk.PhotoImage(Image.fromarray(arr))
    cv.delete("all")
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv._wf_photo = photo   # keep reference — prevents GC


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE _draw_timeline entirely
# (phrase-bar below waveform — now viewport-aware too)
# ─────────────────────────────────────────────────────────────────────────────

def _draw_timeline(self, client, pssi, dur_s, cv):
    import numpy as np
    from PIL import Image, ImageTk

    w = cv.winfo_width()
    h = cv.winfo_height()
    if w < 4 or h < 2:
        return

    VIEW_S    = 30.0
    HEAD_FRAC = 0.33
    pos_s     = client.position if client else None
    if pos_s is None:
        pos_s = 0.0

    view_start = pos_s - HEAD_FRAC * VIEW_S
    head_x     = int(HEAD_FRAC * w)

    arr = np.full((h, w, 3), [26, 26, 42], dtype=np.uint8)

    entries  = pssi.get("entries", []) if pssi else []
    end_beat = (pssi.get("end_beat", 1) or 1) - 1 if pssi else 0

    if entries and end_beat > 0 and dur_s and dur_s > 0:
        for idx, e in enumerate(entries):
            sb = e["beat"] - 1
            eb = (entries[idx + 1]["beat"] - 1) if idx + 1 < len(entries) else end_beat
            t_start = sb / end_beat * dur_s
            t_end   = eb / end_beat * dur_s
            x1 = int((t_start - view_start) / VIEW_S * w)
            x2 = int((t_end   - view_start) / VIEW_S * w)
            x1, x2 = max(0, x1), min(w, max(x1 + 1, x2))
            if x2 > x1:
                ch  = PHRASE_COLORS.get(e["kind_name"], "#555555")
                rgb = (int(ch[1:3], 16), int(ch[3:5], 16), int(ch[5:7], 16))
                arr[1:h - 1, x1:x2] = rgb

    # Playhead
    hx0 = max(0, head_x - 1)
    hx1 = min(w, head_x + 1)
    arr[:, hx0:hx1] = [255, 255, 255]

    photo = ImageTk.PhotoImage(Image.fromarray(arr))
    cv.delete("all")
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv._tl_photo = photo


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE _draw_time_ruler entirely
# (time labels in the viewport window)
# ─────────────────────────────────────────────────────────────────────────────

def _draw_time_ruler(self, dur_s, pos_s, cv):
    w = cv.winfo_width()
    if w < 4:
        return

    VIEW_S    = 30.0
    HEAD_FRAC = 0.33
    if pos_s is None:
        pos_s = 0.0

    view_start = pos_s - HEAD_FRAC * VIEW_S
    view_end   = view_start + VIEW_S
    head_x     = int(HEAD_FRAC * w)

    cv.delete("all")
    cv.create_rectangle(0, 0, w, 18, fill=BG2, outline="")

    # Current position label at playhead
    m, s = divmod(int(max(0, pos_s)), 60)
    cv.create_text(head_x + 4, 9, text=f"{m}:{s:02d}", fill=TEXT,
                   font=("Helvetica Neue", 8), anchor="w")

    # Remaining time (right edge)
    if dur_s and dur_s > 0:
        rem = max(0, int(dur_s - pos_s))
        rm, rs = divmod(rem, 60)
        cv.create_text(w - 2, 9, text=f"-{rm}:{rs:02d}", fill=DIM,
                       font=("Helvetica Neue", 8), anchor="e")

    # Tick marks every 5s within viewport
    interval = 5.0
    t = (int(view_start / interval) + 1) * interval
    while t < view_end:
        if t >= 0 and (dur_s is None or t <= dur_s):
            x  = int((t - view_start) / VIEW_S * w)
            tm, ts = divmod(int(t), 60)
            lbl    = f"{tm}:{ts:02d}" if ts == 0 else f":{ts:02d}"
            cv.create_line(x, 0, x, 6, fill="#333333")
            cv.create_text(x, 13, text=lbl, fill="#444444",
                           font=("Helvetica Neue", 7), anchor="center")
        t += interval

    # Playhead tick
    cv.create_line(head_x, 0, head_x, 18, fill="white", width=1)
