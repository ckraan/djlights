"""
player_state.py - Immutable player state snapshots
"""

from dataclasses import dataclass
from typing import List, Optional

@dataclass(frozen=True)
class Phrase:
    index: int
    beat_start: int
    beat_end: int
    kind: str
    display: str
    fill: bool = False
    fill_beat: int = 0

@dataclass(frozen=True)
class CuePoint:
    index: int
    label: str
    name: str
    type: str
    time_ms: int
    beat: int
    color: tuple
    loop_end_ms: int = 0

@dataclass(frozen=True)
class PlayerSnapshot:
    """Immutable snapshot - safe to pass between threads"""
    player: int

    # Track identity
    track_id: int
    title: str
    artist: str

    # Real-time state (updated every beat)
    beat_number: int        # Absolute beat in track
    beat_within_bar: int    # 1-4
    is_playing: bool
    bpm: float              # Current tempo (with pitch)
    position_ms: float      # Calculated from beats

    # Track structure (loaded once)
    track_bpm: float        # Original track BPM
    duration_ms: float
    phrases: tuple          # tuple for immutability
    cues: tuple

    # Current phrase (calculated from beat_number)
    current_phrase: Optional[Phrase]
    beats_to_next_phrase: int

    # Status flags
    on_air: bool
    is_master: bool
    is_synced: bool
    is_looping: bool

    # Display helpers
    key: str
    genre: str
    mood: str

    # Media
    waveform_url: str
    artwork_url: str

    # Waveform energy (EMA-smoothed per beat)
    bass_energy:   float = 0.0
    mid_energy:    float = 0.0
    treble_energy: float = 0.0

    @property
    def has_track(self) -> bool:
        return self.track_id > 0

    @property
    def time_remaining_ms(self) -> float:
        return max(0, self.duration_ms - self.position_ms)
