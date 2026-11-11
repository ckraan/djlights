"""
djlights — terminal monitor

Shows live Pro DJ Link state:
  - Per-player: track, BPM, key, position, phrase, beat
  - LED strip preview (colour blocks)
  - Waveform colour at playhead
  - Hot cues

Run standalone (with prodj running in main.py) or integrated.
"""
import os
import sys
import time
import math
import colorsys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich.live    import Live
from rich.table   import Table
from rich.text    import Text
from rich.panel   import Panel
from rich.columns import Columns
from rich.console import Console
from rich         import box

from prodj.core.prodj import ProDj
from prodj.pdblib.usbanlz import parse_cue_extended_entries
import engine
import wled

console = Console()

WAVEFORM_FPS = 150

# ── Colour helpers ────────────────────────────────────────────────────────────

def rgb_swatch(r, g, b, width=3) -> Text:
    """A coloured block using terminal true-colour."""
    t = Text("█" * width)
    t.stylize(f"rgb({r},{g},{b})")
    return t


def hue_swatch(hue_deg, sat=0.9, val=0.85, width=3) -> Text:
    r, g, b = colorsys.hsv_to_rgb(hue_deg / 360, sat, val)
    return rgb_swatch(int(r*255), int(g*255), int(b*255), width)


def beat_dots(beat: int) -> Text:
    """  ●○○○  style beat indicator."""
    out = Text()
    for i in range(1, 5):
        if i == beat:
            out.append("●", style="bold white")
        else:
            out.append("○", style="dim white")
    return out


def phrase_badge(phrase: str) -> Text:
    colours = {
        "intro":  "bold deep_sky_blue1",
        "verse":  "bold green",
        "up":     "bold dark_orange",
        "chorus": "bold red",
        "bridge": "bold medium_orchid",
        "outro":  "bold steel_blue",
        "down":   "bold steel_blue",
        "":       "dim white",
    }
    label = phrase.upper() if phrase else "——"
    return Text(f" {label} ", style=colours.get(phrase, "white"))


KEY_NAMES = {
    0: "—",
    1:"1A (Am)",  2:"1B (C)",
    3:"2A (Em)",  4:"2B (G)",
    5:"3A (Bm)",  6:"3B (D)",
    7:"4A (F#m)", 8:"4B (A)",
    9:"5A (C#m)", 10:"5B (E)",
    11:"6A (G#m)",12:"6B (B",
    13:"7A (Ebm)",14:"7B (F#)",
    15:"8A (Bbm)",16:"8B (Db)",
    17:"9A (Fm)", 18:"9B (Ab)",
    19:"10A(Cm)", 20:"10B(Eb)",
    21:"11A(Gm)", 22:"11B(Bb)",
    23:"12A(Dm)", 24:"12B(F)",
}


def pos_bar(pos_s, duration_s, width=20) -> Text:
    if not pos_s or not duration_s or duration_s <= 0:
        return Text("─" * width, style="dim")
    frac  = min(1.0, pos_s / duration_s)
    filled = int(frac * width)
    bar   = Text()
    bar.append("█" * filled,       style="cyan")
    bar.append("─" * (width - filled), style="dim")
    return bar


def led_preview(pixels, width=60) -> Text:
    """Downsample LED strip to `width` colour blocks."""
    n   = len(pixels)
    out = Text()
    for i in range(width):
        idx = int(i * n / width)
        r, g, b = pixels[idx]
        out.append("█", style=f"rgb({r},{g},{b})")
    return out


# ── Phrase lookup from PSSI ───────────────────────────────────────────────────

def _phrase_for_client(client) -> str:
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


# ── Build the display ─────────────────────────────────────────────────────────

def build_layout(p: ProDj, last_pixels) -> Panel:
    clients = sorted(p.cl.clients, key=lambda c: c.player_number)

    # ── Player rows ───────────────────────────────────────────────────────────
    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
                pad_edge=False, expand=True)
    tbl.add_column("P",      width=2,  justify="center")
    tbl.add_column("TRACK",  width=28, no_wrap=True)
    tbl.add_column("BPM",    width=7,  justify="right")
    tbl.add_column("KEY",    width=11)
    tbl.add_column("BEAT",   width=6,  justify="center")
    tbl.add_column("PHRASE", width=9,  justify="center")
    tbl.add_column("POSITION", width=22)
    tbl.add_column("WAVEFORM", width=6, justify="center")

    best_client = None
    best_score  = -1

    for c in clients:
        is_playing = c.play_state in ("playing", "cueing")
        on_air     = getattr(c, "on_air", False)
        score      = (on_air, is_playing, c.bpm or 0)
        if sum(1 for x in score if x) > best_score or best_score < 0:
            best_score  = sum(1 for x in score if x)
            best_client = c

        # Player number badge
        pn_style = "bold green" if on_air else ("white" if is_playing else "dim")
        pn_text  = Text(str(c.player_number), style=pn_style)
        if on_air:
            pn_text.append("◉", style="bold red")

        # Track name
        meta = c.metadata
        if meta:
            title  = str(meta.get("title",  "")) or "—"
            artist = str(meta.get("artist", "")) or ""
            track_text = Text(title[:20], style="white" if is_playing else "dim")
            if artist:
                track_text.append(f"\n{artist[:20]}", style="dim")
        else:
            track_text = Text("—", style="dim")

        # BPM
        bpm_val  = c.bpm or 0
        bpm_text = Text(f"{bpm_val:.1f}" if bpm_val else "—",
                        style="bold yellow" if is_playing else "dim")

        # Key
        key_id   = c.key or 0
        key_text = Text(KEY_NAMES.get(key_id, "?"), style="cyan" if key_id else "dim")

        # Beat indicator
        beat = c.beat if c.beat else 0
        bd   = beat_dots(beat) if is_playing else Text("○○○○", style="dim")

        # Phrase
        phrase = _phrase_for_client(c)
        pb     = phrase_badge(phrase) if is_playing else Text(" —— ", style="dim")

        # Position bar
        pos_s  = c.position
        # Try to get duration from metadata
        dur_s  = None
        if meta:
            try:
                dur_s = float(str(meta.get("duration", 0) or 0))
            except Exception:
                pass
        pos_text = Text()
        if pos_s is not None and is_playing:
            m   = int(pos_s // 60)
            s   = int(pos_s % 60)
            pos_text.append(f"{m}:{s:02d} ", style="cyan")
            pos_text.append_text(pos_bar(pos_s, dur_s or 300, width=14))
        else:
            pos_text = Text("—", style="dim")

        # Waveform colour at playhead
        wv_cell = Text()
        if is_playing and pos_s is not None:
            key_tuple = (c.loaded_player_number, c.loaded_slot, c.track_id)
            cw = p.data.color_waveform_store.get(key_tuple)
            if cw:
                idx = int(pos_s * WAVEFORM_FPS)
                if 0 <= idx < len(cw):
                    v = cw[idx]
                    r = ((v >> 13) & 0x07) * 255 // 7
                    g = ((v >> 10) & 0x07) * 255 // 7
                    b = ((v >>  7) & 0x07) * 255 // 7
                    wv_cell = rgb_swatch(r, g, b, width=4)
                else:
                    wv_cell = Text("····", style="dim")
            else:
                wv_cell = Text("····", style="dim")

        row_style = "" if is_playing else "dim"
        tbl.add_row(pn_text, track_text, bpm_text, key_text, bd, pb, pos_text, wv_cell,
                    style=row_style)

    # ── Hot cues for best player ──────────────────────────────────────────────
    cue_line = Text()
    if best_client:
        cues = engine._hot_cues.get(best_client.player_number, [])
        for cue in cues:
            if cue.get("hot_cue", 0) == 0:
                continue
            n   = cue["hot_cue"]
            r, g, b = cue.get("color_r", 200), cue.get("color_g", 200), cue.get("color_b", 200)
            lbl = cue.get("label", "") or f"#{n}"
            cue_line.append(f" {lbl} ", style=f"bold rgb({r},{g},{b}) on grey11")
            pos_ms = cue.get("time_ms", 0)
            m, s = divmod(int(pos_ms / 1000), 60)
            cue_line.append(f"{m}:{s:02d}  ", style="dim")
        if not cue_line._spans:
            cue_line = Text("no hot cues", style="dim")
    else:
        cue_line = Text("—", style="dim")

    # ── LED strip preview ─────────────────────────────────────────────────────
    if last_pixels is not None:
        led_line = led_preview(last_pixels, width=console.width - 6)
    else:
        led_line = Text("no signal", style="dim")

    # ── PSSI mood + bank for best player ─────────────────────────────────────
    pssi_line = Text()
    if best_client:
        pssi = getattr(best_client, "_pssi", None)
        if pssi:
            mood_names = {1: "HIGH", 2: "MID", 3: "LOW"}
            bank_names = {0:"default",1:"cool",2:"natural",3:"hot",4:"subtle",
                          5:"warm",6:"vivid",7:"club1",8:"club2"}
            mood = mood_names.get(pssi.get("mood", 0), "?")
            bank = bank_names.get(pssi.get("bank", 0), "?")
            n_phrases = len(pssi.get("entries", []))
            pssi_line.append(f"mood:{mood}  bank:{bank}  {n_phrases} phrases", style="cyan")
        else:
            pssi_line = Text("no phrase analysis", style="dim")

    # ── Assemble ──────────────────────────────────────────────────────────────
    from rich.console import Group
    body = Group(
        tbl,
        Text(""),
        Text("CUES  ", style="bold") + cue_line,
        Text("PSSI  ", style="bold") + pssi_line,
        Text(""),
        Text("LEDS  ", style="bold") + led_line,
    )

    return Panel(body, title="[bold cyan]djlights[/bold cyan]", border_style="cyan")


# ── Main ──────────────────────────────────────────────────────────────────────

def _waveform_callback(player_number, p):
    def cb(request, src, slot, item_id, data):
        if data is not None:
            log_msg = f"P{player_number} waveform {len(data)} segments"
        else:
            log_msg = f"P{player_number} waveform not available"
    return cb


def _pssi_callback(player_number, p):
    def cb(request, src, slot, item_id, data):
        if data is None:
            return
        c = p.cl.getClient(player_number)
        if c:
            key_tuple = (c.loaded_player_number, c.loaded_slot, c.track_id)
            beatgrid  = p.data.beatgrid_store.get(key_tuple)
            engine.set_pssi(player_number, c, data, beatgrid)
    return cb


def on_client_change(cl, player_number, p):
    c = cl.getClient(player_number)
    if not c or not c.track_id or c.play_state in ("no_track", "loading_track"):
        return
    pn, slot, tid = c.loaded_player_number, c.loaded_slot, c.track_id
    p.data.get_color_waveform(pn, slot, tid, _waveform_callback(player_number, p))
    p.data.get_pssi(pn, slot, tid, _pssi_callback(player_number, p))


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    p = ProDj()
    p.set_client_change_callback(lambda cl, n: on_client_change(cl, n, p))

    last_pixels = [None]

    def render_loop():
        import numpy as np
        while True:
            clients = sorted(p.cl.clients, key=lambda c: c.player_number)
            playing = [c for c in clients if c.play_state in ("playing","cueing") and c.bpm]
            if playing:
                best = max(playing, key=lambda c: (getattr(c, "on_air", False), c.bpm or 0))
                engine.on_status(best)
                px = engine.render_frame(best, best.player_number, p.data)
            else:
                px = wled.black()
            last_pixels[0] = px
            wled.send_pixels(px)
            time.sleep(1/30)

    t = threading.Thread(target=render_loop, daemon=True)
    t.start()

    p.start()
    p.vcdj_enable()

    with Live(console=console, refresh_per_second=10, screen=True) as live:
        try:
            while True:
                live.update(build_layout(p, last_pixels[0]))
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            p.stop()
