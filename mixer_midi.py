"""
mixer_midi.py — Pioneer DJM-A9 MIDI reader + deck weight calculator.

Self-contained module. No dependencies on wled_ctrl or blt_client.
Import and call get_deck_weights() to get a dict {1: 0.0-1.0, 2: ..., 3: ..., 4: ...}

CC mapping source: Pioneer DJM-A9 MIDI Message List (official PDF)
All CCs on a single configurable MIDI channel (default ch 1 = index 0).
Value range 0–127 for all controls.
"""

import threading
import time
import logging

_log = logging.getLogger(__name__)

# ── DJM-A9 CC map ─────────────────────────────────────────────────────────────

_CC_MAP = {
    # CH: (trim, hi, mid, low, fader)
    1: dict(trim=1,  hi=2,  mid=3,  low=4,  fader=17),
    2: dict(trim=6,  hi=7,  mid=8,  low=9,  fader=18),
    3: dict(trim=12, hi=14, mid=15, low=21, fader=19),
    4: dict(trim=80, hi=81, mid=92, low=82, fader=20),
}

# Reverse lookup: cc_number → (channel, control_name)
_CC_REVERSE = {}
for _ch, _ccs in _CC_MAP.items():
    for _name, _cc in _ccs.items():
        _CC_REVERSE[_cc] = (_ch, _name)

# Center value for EQ knobs (noon = 64 = 0 dB)
_EQ_CENTER = 64
# How far below center the LOW EQ must be to consider bass "cut"
_BASS_CUT_THRESHOLD = 20  # below 44 out of 127


class MixerMidi:
    """
    Connects to a DJM-A9 via USB MIDI and tracks fader/EQ state.
    Call start() once; call get_deck_weights() any time.
    Thread-safe reads.
    """

    def __init__(self, midi_channel: int = 0):
        """
        midi_channel: 0-indexed MIDI channel (default 0 = ch 1).
        The DJM-A9 is configured in the mixer's MIDI settings.
        """
        self.midi_channel = midi_channel
        self._port = None
        self._running = False
        self._thread = None

        # Raw CC values, initialised to center/up
        self._fader = {ch: 127 for ch in range(1, 5)}   # 0=closed 127=open
        self._low   = {ch: _EQ_CENTER for ch in range(1, 5)}
        self._mid   = {ch: _EQ_CENTER for ch in range(1, 5)}
        self._hi    = {ch: _EQ_CENTER for ch in range(1, 5)}
        self._trim  = {ch: _EQ_CENTER for ch in range(1, 5)}

        self._lock = threading.Lock()
        self._last_msg_time = 0.0
        self._connected = False
        self._port_name = ""

    # ── Connection ────────────────────────────────────────────────────────────

    def available_ports(self):
        """Return list of available MIDI input port names."""
        try:
            import mido
            return mido.get_input_names()
        except Exception as e:
            _log.warning("MIDI port list failed: %s", e)
            return []

    def start(self, port_name: str = None) -> bool:
        """
        Open MIDI port and start listening.
        port_name: exact name from available_ports(), or None to auto-detect DJM-A9.
        Returns True if connected.
        """
        if self._running:
            return self._connected

        import mido

        ports = mido.get_input_names()
        if not ports:
            _log.warning("No MIDI input ports found")
            return False

        if port_name is None:
            # Auto-detect: prefer anything with "DJM" or "A9" in name
            for p in ports:
                if "DJM" in p.upper() or "A9" in p.upper():
                    port_name = p
                    break
            if port_name is None:
                port_name = ports[0]  # fallback to first port

        try:
            self._port = mido.open_input(port_name, callback=self._on_msg)
            self._port_name = port_name
            self._running = True
            self._connected = True
            _log.info("MIDI connected: %s", port_name)
            return True
        except Exception as e:
            _log.warning("MIDI open failed (%s): %s", port_name, e)
            return False

    def stop(self):
        self._running = False
        self._connected = False
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    # ── MIDI callback ─────────────────────────────────────────────────────────

    def _on_msg(self, msg):
        if msg.type != "control_change":
            return
        if msg.channel != self.midi_channel:
            return

        cc  = msg.control
        val = msg.value
        self._last_msg_time = time.time()

        lookup = _CC_REVERSE.get(cc)
        if lookup is None:
            return

        ch, name = lookup
        with self._lock:
            if   name == "fader": self._fader[ch] = val
            elif name == "low":   self._low[ch]   = val
            elif name == "mid":   self._mid[ch]   = val
            elif name == "hi":    self._hi[ch]    = val
            elif name == "trim":  self._trim[ch]  = val

    # ── Deck weights ─────────────────────────────────────────────────────────

    def get_deck_weights(self) -> dict:
        """
        Returns {1: weight, 2: weight, 3: weight, 4: weight} where
        weight is 0.0–1.0 representing how "dominant" each deck is.

        Algorithm:
          base   = fader / 127
          bass_ok = LOW EQ within cut threshold of center
          weight  = base * (1.0 if bass_ok else 0.15)

        The deck with the highest weight owns the lights.
        """
        with self._lock:
            faders = dict(self._fader)
            lows   = dict(self._low)

        weights = {}
        for ch in range(1, 5):
            fader_norm = faders[ch] / 127.0
            bass_ok    = lows[ch] >= (_EQ_CENTER - _BASS_CUT_THRESHOLD)
            weights[ch] = fader_norm * (1.0 if bass_ok else 0.15)

        return weights

    def get_raw(self) -> dict:
        """Return raw CC values for display."""
        with self._lock:
            return {
                ch: {
                    "fader": self._fader[ch],
                    "hi":    self._hi[ch],
                    "mid":   self._mid[ch],
                    "low":   self._low[ch],
                    "trim":  self._trim[ch],
                }
                for ch in range(1, 5)
            }

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def port_name(self) -> str:
        return self._port_name

    @property
    def seconds_since_last_msg(self) -> float:
        if self._last_msg_time == 0:
            return float("inf")
        return time.time() - self._last_msg_time
