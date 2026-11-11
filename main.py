"""
djlights — main entry point

Direct Pro DJ Link via python-prodj-link (no BLT middleware).

Data flow:
  CDJs → (broadcast UDP 50000/50001/50002) → ProDj listener
       → (TCP DB queries port 1051) → metadata, waveform, PSSI, cues
       → engine.py → wled.py → WLED DDP UDP
"""
import os
import sys
import time
import logging
import threading

# Add prodj to path (local copy)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prodj.core.prodj import ProDj
from prodj.pdblib.usbanlz import parse_cue_extended_entries

import engine
import wled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

FPS = 30
FRAME_TIME = 1.0 / FPS

# Network interface (set to your DJ network interface, or "" to auto-detect)
INTERFACE = ""

# ── Callbacks from prodj ──────────────────────────────────────────────────────

def on_client_keepalive(cl, player_number):
    log.info("Player %d online", player_number)


def on_client_change(cl, player_number):
    c = cl.getClient(player_number)
    if c is None:
        return

    # Auto-request data when track loads
    if c.track_id and c.track_id != 0 and c.play_state not in ("no_track", "loading_track"):
        pn   = c.loaded_player_number
        slot = c.loaded_slot
        tid  = c.track_id

        # Request color scroll waveform (PWV5) — for real-time color
        p.data.get_color_waveform(pn, slot, tid, _waveform_callback(player_number))

        # Request phrase analysis (PSSI)
        p.data.get_pssi(pn, slot, tid, _pssi_callback(player_number))

        # Request cue list (extended, for hot cue colors)
        # We use the standard metadata path which includes cue data


def _waveform_callback(player_number):
    def cb(request, source_player, slot, item_id, data):
        if data is not None:
            log.info("P%d color waveform: %d segments", player_number, len(data))
        else:
            log.warning("P%d color waveform not available (NXS1 or no EXT file)", player_number)
    return cb


def _pssi_callback(player_number):
    def cb(request, source_player, slot, item_id, data):
        if data is None:
            log.info("P%d PSSI not available (no phrase analysis on track)", player_number)
            return
        c = p.cl.getClient(player_number)
        if c:
            beatgrid_key = (c.loaded_player_number, c.loaded_slot, c.track_id)
            beatgrid = p.data.beatgrid_store.get(beatgrid_key)
            engine.set_pssi(player_number, c, data, beatgrid)
    return cb


# ── Best player selection ─────────────────────────────────────────────────────

def _best_player():
    """Return the most relevant playing client."""
    clients = [c for c in p.cl.clients if c.play_state in ("playing", "cueing") and c.bpm]
    if not clients:
        clients = [c for c in p.cl.clients if c.bpm]
    if not clients:
        return None, None

    best = max(clients, key=lambda c: (
        c.on_air if hasattr(c, "on_air") else False,
        c.bpm or 0,
        (c.position or 0),
    ))
    return best, best.player_number


# ── Render loop ───────────────────────────────────────────────────────────────

def render_loop():
    log.info("Render loop started at %d FPS", FPS)
    next_frame = time.time()

    while True:
        now = time.time()
        if now < next_frame:
            time.sleep(next_frame - now)
        next_frame += FRAME_TIME

        player, player_number = _best_player()

        if player:
            engine.on_status(player)
            pixels = engine.render_frame(player, player_number, p.data)
        else:
            pixels = wled.black()

        wled.send_pixels(pixels)


# ── Main ──────────────────────────────────────────────────────────────────────

p = ProDj()
p.set_client_keepalive_callback(on_client_keepalive)
p.set_client_change_callback(on_client_change)

if __name__ == "__main__":
    log.info("djlights starting — direct Pro DJ Link")

    p.start()
    p.vcdj_enable()
    log.info("Virtual CDJ enabled, listening for players...")

    render_thread = threading.Thread(target=render_loop, name="render", daemon=True)
    render_thread.start()

    try:
        p.join()
    except KeyboardInterrupt:
        log.info("Shutting down")
        p.stop()
