# Beat Link Trigger integration

← [Back to README](../README.md)

[Beat Link Trigger](https://github.com/Deep-Symmetry/beat-link-trigger) (BLT)
speaks Pro DJ Link to the CDJs. It gives djlights the track metadata, beat
grids, rekordbox phrase analysis and waveforms. djlights adds a layer of
Clojure *expressions* on top of BLT. That layer turns this data into a compact
OSC and UDP stream for the Python app.

```
blt/
├── djlights.blt.template   BLT show file (paths filled in by install.sh → djlights.blt)
└── vp_blt_code.clj         All djlights expression code, loaded at startup
```

## How it is wired into BLT

The show file only holds small shims. All the real logic lives in
`vp_blt_code.clj`, so you can edit it as a normal file in your editor.

| BLT slot | Expression |
|---|---|
| **Global Setup** | `(load-file "<repo>/blt/vp_blt_code.clj")`, wrapped in `try` so a load error is printed instead of crashing BLT |
| **Global Shutdown** | Closes the OSC client and the UDP socket |
| **Trigger "djlights — Player 1…4"**, *Beat* | `((:vp-beat-fn! @globals) beat status track-time-reached)` |
| **Trigger "djlights — Player 1…4"**, *Tracked Update* | `((:vp-update-fn! @globals) status)` |

BLT 8 evaluates trigger expressions in private namespaces. That's why the code
registers its entry points in BLT's shared `globals` atom (`:vp-beat-fn!`,
`:vp-update-fn!`) and the triggers look them up there.

**After editing `vp_blt_code.clj`**, reopen `blt/djlights.blt` in BLT. The
triggers don't need to change.

### Configuration at the top of `vp_blt_code.clj`

```clojure
(def vp-cdj-iface "")      ; network interface of the CDJs, e.g. "en5" ("" = let BLT choose)
(def vp-osc-port 9000)     ; must match SETTINGS → OSC port
(def vp-udp-port 9001)     ; must match SETTINGS → UDP port
```

`install.sh` sets `vp-cdj-iface`. The SETTINGS tab updates the two ports when
you save.

## What the code does

| Function / block | Responsibility |
|---|---|
| Startup block | Starts BLT's finders in the background (metadata, beat grid, colour waveform, `AnalysisTagFinder` for phrases). Starts BLT's web server on **:17081** and an **nREPL on :17001** (used by the watchdog to un-stick frozen triggers). |
| Device listener | When a CDJ disappears from the network, clears its state and sends `/blt/unload` |
| `vp-osc!`, `vp-udp!` | Send helpers: OSC to `127.0.0.1:vp-osc-port`, UDP to `127.0.0.1:vp-udp-port` |
| `vp-phrases`, `vp-analysis-phrases`, `vp-tag-from-atf`, `vp-phrase-kind` | Read the rekordbox phrase analysis (PSSI) and map it to `intro · verse · up · chorus · down · bridge · outro`, taking the track's mood (high / mid / low) into account |
| `vp-cues` | Hot cues, memory cues and loops (`type`: `hot_cue` · `memory` · `loop`) with name, colour, time and beat |
| `vp-energy` | 3-band energy (bass / mid / treble) at the playhead, from the colour waveform |
| `vp-dump-meta!` | On track load, sends the full track description as JSON over UDP. Writes the artwork to `/tmp/vp_art_P<n>.jpg`. |
| `vp-beat-entry!` | On **every beat**, sends `/blt/beat`. Sends `/blt/phrase` when the phrase index changes, and `/blt/track-loading` on a new track. Writes `/tmp/vp_state.json` asynchronously. |
| `vp-update-entry!` | Every ~200 ms, sends `/blt/status` with the interpolated position and the on-air / master / sync / loop flags |
| Phrase overlay | Serves `http://127.0.0.1:17082/`, a transparent overlay showing the current phrase and beat countdown (works as an OBS browser source, 420×180) |
| Interface bind | If no CDJs are found 5 s after start and `vp-cdj-iface` is set, restarts BLT's DeviceFinder and VirtualCdj once on that interface |

## Message protocol

All messages go to `127.0.0.1`. Player numbers (`dev`) are 1–4.

### OSC (port 9000)

| Address | Sent | Arguments (in order) |
|---|---|---|
| `/blt/beat` | every beat | `dev` i, `bpm` f, `pitch` f, `pos_ms` f, `beat_number` i, `beat_in_bar` i (1–4), `on_air` i, `playing` i, `master` i, `sync` i, `bass` f, `mid` f, `treble` f, `amp` f, `phrase_idx` i, `phrase_kind` s, `phrase_display` s, `beat_in_phrase` i, `phrase_section` s, `phrase_end_beat` i, `beats_to_change` i |
| `/blt/status` | ~5×/s | `dev`, `bpm`, `pitch`, `pos_ms`, `remain_ms`, `on_air`, `playing`, `master`, `sync`, `looping` |
| `/blt/phrase` | phrase change | `dev`, `phrase_idx`, `kind`, `display`, `beat_start`, `beat_end`, `phrase_section` |
| `/blt/track-loading` | new track | `dev`, `track_id` |
| `/blt/unload` | track ejected / CDJ gone | `dev` |

Flags are `0` or `1`. Energies are floats from 0 to 1.

### UDP JSON (port 9001)

There is one packet per track load, and it is resent while the phrase analysis
is still pending:

```json
{
  "type": "track_meta",
  "player": 1,
  "track_id": 123,
  "title": "…", "artist": "…", "album": "…", "label": "…", "genre": "…", "comment": "…",
  "bpm": 124.0, "key": "Am", "duration_ms": 372000, "rating": 4, "color_hex": "#ff0000",
  "mood": "high", "bank": "…",
  "artwork_path": "/tmp/vp_art_P1.jpg",
  "waveform_url": "/wave-detail/1?width=500",
  "waveform_overview_url": "/wave-preview/1",
  "phrases": [
    { "index": 0, "kind": "intro", "display": "Intro", "beat_start": 1, "beat_end": 65,
      "fill": false, "fill_beat": 0 }
  ],
  "cues": [
    { "index": 1, "label": "A", "name": "Drop", "type": "hot_cue", "time_ms": 61000, "beat": 129,
      "color": [255, 0, 0], "loop_end_ms": 0 }
  ],
  "phrases_pending": false
}
```

`phrases_pending: true` means BLT's analysis isn't ready yet. The app then polls
every 500 ms (up to 10 times). Phrases that arrive are cached in
`metadata_cache.json`, so a track loads instantly the next time.

### Other channels

| Channel | Purpose |
|---|---|
| `/tmp/vp_state.json` | Per-player phrase/beat state, rewritten every beat. Used as a backup source. |
| `http://localhost:17081/params.json` | BLT's own web server. Polled as a fallback and for loop status. |

## The Python side: `blt_client.py`

`BLTClient` listens on all of the channels above. It keeps one mutable state per
player, behind a lock. Each frame the render loop asks for
`get_snapshot(player)` and gets back an immutable `PlayerSnapshot`
(`player_state.py`).

When sources disagree, the client trusts them in this order:

1. **OSC `/blt/beat`**: authoritative for the beat number and play state
2. **UDP `track_meta`**: authoritative for phrases and cues
3. **`/tmp/vp_state.json`**: backup for phrase and beat timing
4. **HTTP `params.json`**: fallback, and the source for loop status

The client also has these robustness features:
- a phrase retry loop
- a beat "heartbeat" that dead-reckons the position when OSC goes quiet
- UDP jitter tracking
- a one-line health summary in `djlights_diag.log`

## Testing without the app

Watch the raw OSC stream with any OSC monitor, for example:

```bash
venv/bin/python -c "
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
d = Dispatcher(); d.set_default_handler(lambda a, *x: print(a, x))
BlockingOSCUDPServer(('0.0.0.0', 9000), d).serve_forever()"
```

(Close djlights first: only one program can listen on port 9000 at a time.)
