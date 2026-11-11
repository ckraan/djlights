"""
djlights — desktop app
macOS window showing live Pro DJ Link state + LED preview.
"""
import os
import sys
import time
import math
import colorsys
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dearpygui.dearpygui as dpg
import numpy as np

from prodj.core.prodj import ProDj
import engine
import wled

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")

# ── Constants ─────────────────────────────────────────────────────────────────
WAVEFORM_FPS   = 150
WIN_W, WIN_H   = 1100, 680
LED_STRIP_H    = 28
PLAYER_PANEL_H = 140
N_PLAYERS      = 4

KEY_NAMES = {
    0:"—",        1:"Am / 1A",  2:"C / 1B",
    3:"Em / 2A",  4:"G / 2B",   5:"Bm / 3A",
    6:"D / 3B",   7:"F#m/ 4A",  8:"A / 4B",
    9:"C#m/ 5A", 10:"E / 5B",  11:"G#m/ 6A",
    12:"B / 6B", 13:"Ebm/ 7A", 14:"F#/ 7B",
    15:"Bbm/ 8A",16:"Db/ 8B",  17:"Fm / 9A",
    18:"Ab/ 9B", 19:"Cm /10A", 20:"Eb /10B",
    21:"Gm /11A",22:"Bb /11B", 23:"Dm /12A",
    24:"F /12B",
}

PHRASE_COLORS = {
    "intro":  (0.18, 0.52, 0.95, 1.0),
    "verse":  (0.22, 0.82, 0.32, 1.0),
    "up":     (0.98, 0.60, 0.10, 1.0),
    "chorus": (0.95, 0.22, 0.22, 1.0),
    "bridge": (0.72, 0.28, 0.95, 1.0),
    "outro":  (0.30, 0.55, 0.85, 1.0),
    "down":   (0.30, 0.55, 0.85, 1.0),
    "":       (0.35, 0.35, 0.35, 1.0),
}

PHRASE_LABELS = {
    "intro": "INTRO", "verse": "VERSE", "up": "UP",
    "chorus": "CHORUS", "bridge": "BRIDGE", "outro": "OUTRO",
    "down": "DOWN", "": "——",
}

DARK_BG       = (18, 18, 22, 255)
PANEL_BG      = (26, 26, 32, 255)
PANEL_BORDER  = (48, 48, 60, 255)
TEXT_DIM      = (100, 100, 120, 255)
TEXT_BRIGHT   = (220, 220, 240, 255)
ACCENT_CYAN   = (0, 195, 220, 255)
ACCENT_RED    = (220, 50, 50, 255)
BEAT_ACTIVE   = (255, 255, 255, 255)
BEAT_INACTIVE = (55, 55, 70, 255)

# ── State shared between render thread and UI thread ─────────────────────────
_last_pixels: np.ndarray = wled.black()
_prodj: ProDj = None

# ── Colour helpers ────────────────────────────────────────────────────────────

def hsv_to_dpg(h_deg, s, v, a=1.0):
    r, g, b = colorsys.hsv_to_rgb(h_deg / 360, s, v)
    return (int(r*255), int(g*255), int(b*255), int(a*255))


def phrase_for_client(client):
    pssi = getattr(client, "_pssi", None)
    if not pssi:
        return ""
    beat_n = client.beat_count
    if not beat_n or beat_n <= 0:
        return ""
    phrase = ""
    for e in pssi.get("entries", []):
        if e["beat"] <= beat_n:
            phrase = e["kind_name"]
        else:
            break
    return phrase


def waveform_color_for(client):
    """Returns (r,g,b) 0-255 or None."""
    pos_s = client.position
    if pos_s is None:
        return None
    key = (client.loaded_player_number, client.loaded_slot, client.track_id)
    cw  = _prodj.data.color_waveform_store.get(key) if _prodj else None
    if not cw:
        return None
    idx = int(pos_s * WAVEFORM_FPS)
    if not (0 <= idx < len(cw)):
        return None
    v = cw[idx]
    r = ((v >> 13) & 0x07) * 255 // 7
    g = ((v >> 10) & 0x07) * 255 // 7
    b = ((v >>  7) & 0x07) * 255 // 7
    return (r, g, b)


# ── Drawing helpers ───────────────────────────────────────────────────────────

def draw_rounded_rect(draw_list, x, y, w, h, color, rounding=6):
    dpg.draw_rectangle((x, y), (x+w, y+h), color=color, fill=color,
                        rounding=rounding, parent=draw_list)


def draw_text(draw_list, x, y, text, color=TEXT_BRIGHT, size=14):
    dpg.draw_text((x, y), text, color=color, size=size, parent=draw_list)


# ── Build UI ──────────────────────────────────────────────────────────────────

def build_ui():
    dpg.create_context()
    dpg.create_viewport(title="djlights", width=WIN_W, height=WIN_H,
                        resizable=False)
    dpg.setup_dearpygui()

    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,    DARK_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,     PANEL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_Border,      PANEL_BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_Text,        TEXT_BRIGHT)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,    10, 10)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,       8, 6)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,     6)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,     4)
    dpg.bind_theme(global_theme)

    with dpg.window(tag="main", label="djlights", no_title_bar=True,
                    no_move=True, no_resize=True, no_collapse=True,
                    pos=(0, 0), width=WIN_W, height=WIN_H):

        # Title bar
        with dpg.drawlist(width=WIN_W, height=36, tag="title_bar"):
            dpg.draw_rectangle((0,0),(WIN_W,36), color=DARK_BG, fill=DARK_BG)
            dpg.draw_text((14, 8), "djlights", color=ACCENT_CYAN, size=20)
            dpg.draw_text((120, 12), "Pro DJ Link", color=TEXT_DIM, size=14)

        dpg.add_spacer(height=4)

        # Player panels row
        with dpg.group(horizontal=True, tag="players_row"):
            for i in range(1, N_PLAYERS+1):
                with dpg.child_window(tag=f"player_{i}", width=(WIN_W-40)//4,
                                      height=PLAYER_PANEL_H, border=True):
                    with dpg.drawlist(tag=f"draw_{i}",
                                      width=(WIN_W-40)//4 - 4,
                                      height=PLAYER_PANEL_H - 4):
                        _draw_player_empty(f"draw_{i}", i)

        dpg.add_spacer(height=6)

        # PSSI / phrase info bar
        with dpg.child_window(tag="phrase_bar", width=WIN_W-20, height=48, border=True):
            with dpg.drawlist(tag="draw_phrase", width=WIN_W-24, height=44):
                pass

        dpg.add_spacer(height=6)

        # Hot cues bar
        with dpg.child_window(tag="cues_bar", width=WIN_W-20, height=48, border=True):
            with dpg.drawlist(tag="draw_cues", width=WIN_W-24, height=44):
                pass

        dpg.add_spacer(height=6)

        # LED strip
        with dpg.child_window(tag="led_panel", width=WIN_W-20, height=LED_STRIP_H+16,
                               border=True):
            with dpg.drawlist(tag="draw_led", width=WIN_W-24, height=LED_STRIP_H+8):
                pass

    dpg.set_primary_window("main", True)
    dpg.show_viewport()


def _draw_player_empty(draw_tag, player_n):
    W = (WIN_W-40)//4 - 4
    H = PLAYER_PANEL_H - 4
    dpg.draw_text((8, 6),  f"P{player_n}", color=TEXT_DIM, size=16, parent=draw_tag)
    dpg.draw_text((8, 28), "no player",   color=TEXT_DIM, size=12, parent=draw_tag)


# ── Update functions ──────────────────────────────────────────────────────────

def update_player(player_n: int, client=None):
    tag = f"draw_{player_n}"
    if not dpg.does_item_exist(tag):
        return
    dpg.delete_item(tag, children_only=True)

    W = (WIN_W-40)//4 - 4
    H = PLAYER_PANEL_H - 4
    pad = 8

    if client is None or client.play_state == "no_track":
        dpg.draw_rectangle((0,0),(W,H), color=PANEL_BG, fill=PANEL_BG, parent=tag)
        dpg.draw_text((pad, 8), f"P{player_n}", color=TEXT_DIM, size=16, parent=tag)
        dpg.draw_text((pad, 30), "no track", color=TEXT_DIM, size=12, parent=tag)
        return

    is_playing = client.play_state in ("playing", "cueing")
    on_air     = getattr(client, "on_air", False)

    # Background — dim if not playing
    bg = PANEL_BG if is_playing else (20, 20, 24, 255)
    dpg.draw_rectangle((0,0),(W,H), color=bg, fill=bg, parent=tag)

    # On-air indicator strip at top
    if on_air:
        dpg.draw_rectangle((0,0),(W,4), color=ACCENT_RED, fill=ACCENT_RED, parent=tag)

    # Player number
    pn_col = ACCENT_RED if on_air else (ACCENT_CYAN if is_playing else TEXT_DIM)
    dpg.draw_text((pad, 8), f"P{player_n}", color=pn_col, size=16, parent=tag)

    # Track title + artist
    meta   = client.metadata
    title  = str(meta.get("title",  "—")) if meta else "loading…"
    artist = str(meta.get("artist", ""))  if meta else ""
    title  = title[:22] if len(title) > 22 else title
    artist = artist[:22] if len(artist) > 22 else artist
    t_col  = TEXT_BRIGHT if is_playing else TEXT_DIM
    dpg.draw_text((32, 8),  title,  color=t_col, size=13, parent=tag)
    dpg.draw_text((32, 24), artist, color=TEXT_DIM, size=11, parent=tag)

    # BPM
    bpm = client.bpm or 0
    bpm_str = f"{bpm:.1f}" if bpm else "—"
    dpg.draw_text((pad, 44), "BPM", color=TEXT_DIM, size=11, parent=tag)
    dpg.draw_text((pad, 57), bpm_str, color=(255,210,50,255) if is_playing else TEXT_DIM,
                  size=18, parent=tag)

    # Key
    key_id  = client.key or 0
    key_str = KEY_NAMES.get(key_id, "?")
    dpg.draw_text((70, 44), "KEY", color=TEXT_DIM, size=11, parent=tag)
    dpg.draw_text((70, 57), key_str, color=ACCENT_CYAN if key_id else TEXT_DIM,
                  size=13, parent=tag)

    # Beat dots
    beat = client.beat if client.beat else 0
    bx   = pad
    by   = 82
    dpg.draw_text((bx, by-2), "BEAT", color=TEXT_DIM, size=10, parent=tag)
    for i in range(1, 5):
        dot_x = bx + (i-1)*18
        dot_y = by + 12
        col   = BEAT_ACTIVE if (i == beat and is_playing) else BEAT_INACTIVE
        dpg.draw_circle((dot_x+6, dot_y+6), 5, color=col, fill=col, parent=tag)

    # Waveform colour swatch
    wv = waveform_color_for(client) if is_playing else None
    if wv:
        r, g, b = wv
        swatch_x = W - 40
        dpg.draw_text((swatch_x-2, 44), "WV", color=TEXT_DIM, size=10, parent=tag)
        dpg.draw_rectangle((swatch_x, 56), (swatch_x+32, 78),
                            color=(r,g,b,255), fill=(r,g,b,255),
                            rounding=3, parent=tag)

    # Position bar
    pos_s = client.position
    if pos_s is not None:
        dur_s = None
        if meta:
            try:
                dur_s = float(str(meta.get("duration", 0) or 0))
            except Exception:
                pass
        dur_s  = dur_s or 300.0
        frac   = min(1.0, pos_s / dur_s)
        bar_x  = pad
        bar_y  = H - 18
        bar_w  = W - pad*2
        # Track bar background
        dpg.draw_rectangle((bar_x, bar_y), (bar_x+bar_w, bar_y+8),
                            color=(40,40,52,255), fill=(40,40,52,255),
                            rounding=2, parent=tag)
        # Progress
        filled = max(4, int(frac * bar_w))
        dpg.draw_rectangle((bar_x, bar_y), (bar_x+filled, bar_y+8),
                            color=ACCENT_CYAN, fill=ACCENT_CYAN,
                            rounding=2, parent=tag)
        # Time text
        m = int(pos_s // 60)
        s = int(pos_s % 60)
        dpg.draw_text((bar_x, bar_y-14), f"{m}:{s:02d}",
                      color=TEXT_DIM, size=11, parent=tag)


def update_phrase_bar(best_client=None):
    tag = "draw_phrase"
    if not dpg.does_item_exist(tag):
        return
    dpg.delete_item(tag, children_only=True)

    W = WIN_W - 24
    H = 44

    dpg.draw_text((8, 6), "PHRASE ANALYSIS", color=TEXT_DIM, size=11, parent=tag)

    if not best_client:
        dpg.draw_text((8, 22), "no player", color=TEXT_DIM, size=12, parent=tag)
        return

    pssi = getattr(best_client, "_pssi", None)
    if not pssi:
        dpg.draw_text((8, 22), "no phrase data  (track not analyzed in rekordbox)",
                      color=TEXT_DIM, size=12, parent=tag)
        return

    entries   = pssi.get("entries", [])
    end_beat  = pssi.get("end_beat", 1) or 1
    beat_n    = best_client.beat_count or 0
    mood_map  = {1:"HIGH", 2:"MID", 3:"LOW"}
    bank_map  = {0:"default",1:"cool",2:"natural",3:"hot",
                 4:"subtle",5:"warm",6:"vivid",7:"club1",8:"club2"}
    mood_str  = mood_map.get(pssi.get("mood",0), "?")
    bank_str  = bank_map.get(pssi.get("bank",0), "default")
    dpg.draw_text((8, 22), f"mood:{mood_str}  bank:{bank_str}  {len(entries)} phrases",
                  color=ACCENT_CYAN, size=12, parent=tag)

    # Draw phrase timeline across the bar
    tl_x = 200
    tl_w = W - tl_x - 10
    tl_y = 14
    tl_h = 20
    dpg.draw_rectangle((tl_x, tl_y), (tl_x+tl_w, tl_y+tl_h),
                        color=(35,35,45,255), fill=(35,35,45,255), parent=tag)

    for idx, e in enumerate(entries):
        start = e["beat"]
        end   = entries[idx+1]["beat"] if idx+1 < len(entries) else end_beat
        x1    = tl_x + int((start-1) / max(end_beat-1, 1) * tl_w)
        x2    = tl_x + int((end-1)   / max(end_beat-1, 1) * tl_w)
        col   = PHRASE_COLORS.get(e["kind_name"], (0.5,0.5,0.5,1.0))
        rgba  = (int(col[0]*255), int(col[1]*255), int(col[2]*255), 200)
        dpg.draw_rectangle((x1+1, tl_y+1), (x2-1, tl_y+tl_h-1),
                            color=rgba, fill=rgba, rounding=2, parent=tag)

    # Playhead
    if beat_n > 0 and end_beat > 1:
        ph_x = tl_x + int((beat_n-1) / (end_beat-1) * tl_w)
        dpg.draw_line((ph_x, tl_y-2), (ph_x, tl_y+tl_h+2),
                      color=(255,255,255,200), thickness=2, parent=tag)

    # Current phrase label
    current = phrase_for_client(best_client)
    col     = PHRASE_COLORS.get(current, (0.5,0.5,0.5,1.0))
    rgba    = (int(col[0]*255), int(col[1]*255), int(col[2]*255), 255)
    label   = PHRASE_LABELS.get(current, "——")
    dpg.draw_text((tl_x - 70, 22), label, color=rgba, size=13, parent=tag)


def update_cues_bar(best_client=None):
    tag = "draw_cues"
    if not dpg.does_item_exist(tag):
        return
    dpg.delete_item(tag, children_only=True)

    W = WIN_W - 24
    dpg.draw_text((8, 6), "HOT CUES", color=TEXT_DIM, size=11, parent=tag)

    cues = engine._hot_cues.get(best_client.player_number if best_client else -1, [])
    hot  = [c for c in cues if c.get("hot_cue", 0) > 0]

    if not hot:
        dpg.draw_text((8, 22), "no hot cues", color=TEXT_DIM, size=12, parent=tag)
        return

    cx = 8
    for cue in hot[:8]:
        r = cue.get("color_r", 180)
        g = cue.get("color_g", 180)
        b = cue.get("color_b", 180)
        lbl     = (cue.get("label", "") or f"#{cue['hot_cue']}")[:8]
        pos_ms  = cue.get("time_ms", 0)
        m, s    = divmod(int(pos_ms / 1000), 60)
        time_str = f"{m}:{s:02d}"

        # Coloured pill
        pill_w = max(50, len(lbl)*9 + 16)
        dpg.draw_rectangle((cx, 20), (cx+pill_w, 38),
                            color=(r,g,b,60), fill=(r,g,b,60), rounding=4, parent=tag)
        dpg.draw_rectangle((cx, 20), (cx+pill_w, 38),
                            color=(r,g,b,200), fill=(0,0,0,0), rounding=4, parent=tag)
        dpg.draw_text((cx+6, 22), lbl, color=(r,g,b,255), size=12, parent=tag)
        dpg.draw_text((cx+6, 8), time_str, color=TEXT_DIM, size=10, parent=tag)
        cx += pill_w + 12


def update_led_strip(pixels: np.ndarray):
    tag = "draw_led"
    if not dpg.does_item_exist(tag):
        return
    dpg.delete_item(tag, children_only=True)

    W     = WIN_W - 24
    H     = LED_STRIP_H
    n_led = len(pixels)
    n_seg = W   # one pixel per horizontal pixel

    y1 = 4
    y2 = y1 + H

    for i in range(n_seg):
        led_idx = int(i * n_led / n_seg)
        r, g, b = int(pixels[led_idx, 0]), int(pixels[led_idx, 1]), int(pixels[led_idx, 2])
        dpg.draw_line((i, y1), (i, y2), color=(r, g, b, 255), parent=tag)


# ── Render / update loop ──────────────────────────────────────────────────────

def _best_client():
    if _prodj is None:
        return None
    clients  = _prodj.cl.clients
    playing  = [c for c in clients if c.play_state in ("playing","cueing") and c.bpm]
    if not playing:
        playing = [c for c in clients if c.bpm]
    if not playing:
        return None
    return max(playing, key=lambda c: (getattr(c,"on_air",False), c.bpm or 0, c.position or 0))


def render_thread_fn():
    global _last_pixels
    while True:
        best = _best_client()
        if best:
            engine.on_status(best)
            px = engine.render_frame(best, best.player_number, _prodj.data)
        else:
            px = wled.black()
        _last_pixels = px
        wled.send_pixels(px)
        time.sleep(1/30)


def ui_update_loop():
    """Called every frame by DPG render loop."""
    if _prodj is None:
        return

    clients = {c.player_number: c for c in _prodj.cl.clients}
    best    = _best_client()

    for i in range(1, N_PLAYERS+1):
        update_player(i, clients.get(i))

    update_phrase_bar(best)
    update_cues_bar(best)
    update_led_strip(_last_pixels)


# ── ProDj callbacks ───────────────────────────────────────────────────────────

def _on_client_change(cl, player_number):
    c = cl.getClient(player_number)
    if not c or not c.track_id or c.play_state in ("no_track","loading_track"):
        return
    pn, slot, tid = c.loaded_player_number, c.loaded_slot, c.track_id

    def wv_cb(req, src, sl, iid, data):
        if data:
            log.info("P%d waveform: %d segs", player_number, len(data))

    def pssi_cb(req, src, sl, iid, data):
        if data is None:
            return
        key   = (c.loaded_player_number, c.loaded_slot, c.track_id)
        bg    = _prodj.data.beatgrid_store.get(key)
        engine.set_pssi(player_number, c, data, bg)

    _prodj.data.get_color_waveform(pn, slot, tid, wv_cb)
    _prodj.data.get_pssi(pn, slot, tid, pssi_cb)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build window first — always opens immediately
    build_ui()

    # Start ProDj + render in background threads
    def start_prodj():
        global _prodj
        _prodj = ProDj()
        _prodj.set_client_change_callback(_on_client_change)
        try:
            # Detect LAN interface and bind to it specifically —
            # avoids conflict with PRO DJ LINK Bridge on 127.0.0.1:50000
            import netifaces as ni
            own_ip = own_mask = own_mac = None
            for iface in ni.interfaces():
                ifa = ni.ifaddresses(iface)
                if ni.AF_INET in ifa and ni.AF_LINK in ifa:
                    ip = ifa[ni.AF_INET][0].get('addr', '')
                    if ip and not ip.startswith('127.'):
                        own_ip   = ip
                        own_mask = ifa[ni.AF_INET][0].get('netmask', '')
                        own_mac  = ifa[ni.AF_LINK][0].get('addr', '')
                        log.info("Network interface: %s (%s)", ip, iface)
                        break
            if own_ip:
                _prodj.keepalive_ip = own_ip
                _prodj.beat_ip      = own_ip
                _prodj.status_ip    = own_ip
            _prodj.start()
            if own_ip:
                _prodj.vcdj.set_interface_data(own_ip, own_mask, own_mac)
            _prodj.vcdj_enable()
            log.info("ProDj started, virtual CDJ enabled")
        except Exception as e:
            log.error("ProDj start failed: %s", e)

    threading.Thread(target=start_prodj, name="prodj-init", daemon=True).start()
    threading.Thread(target=render_thread_fn, name="render", daemon=True).start()

    while dpg.is_dearpygui_running():
        ui_update_loop()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()
    if _prodj:
        _prodj.stop()
