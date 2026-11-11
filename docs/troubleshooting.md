# Troubleshooting

← [Back to README](../README.md)

## Installation

| Symptom | Fix |
|---|---|
| `no Python 3.10+ with tkinter found` | `brew install python@3.12 python-tk@3.12`, then run `./install.sh` again |
| `pip` fails on `pyobjc` or `sqlcipher3` | You're not on macOS, or you're using a very new Python. Use Python 3.10–3.12 on macOS. |
| `run.sh` says *Run ./install.sh first* | The `venv/` folder is missing. Run `./install.sh`. |

## Beat Link Trigger

| Symptom | Fix |
|---|---|
| BLT console: `[VP] load-file ERROR` | The path in `blt/djlights.blt` is wrong, for example because you moved the folder. Run `./install.sh` and reopen the show. |
| BLT finds no CDJs | The Mac and the CDJs must be on the same subnet. Run `networksetup -listallhardwareports`, then `./install.sh <iface>`, then reopen the show. |
| Track shows, but no phrases | The track was analysed without phrase analysis. Re-analyse it in rekordbox and re-export it. |
| Phrases arrive late | Normal on the first load of a track: the app retries for up to ~5 s. After that, `metadata_cache.json` makes it instant. |

## App

| Symptom | Fix |
|---|---|
| CDJ MONITOR stays empty | Check that BLT runs with the djlights show and that nothing else uses ports 9000/9001 (`lsof -nP -iUDP:9000`). The ports in SETTINGS must match `vp-osc-port`/`vp-udp-port` in `blt/vp_blt_code.clj`. |
| Changed a port and now nothing arrives | Restart djlights **and** reopen the show in BLT. |
| LEDs don't react | WLED: allow realtime (DDP) and check the IP, the LED count and the DDP port (4048). Try **▶ DEMO** to rule out BLT. |
| Choppy output | Lower the FPS in SETTINGS. Use wired ethernet for the WLED controllers. Check `djlights_diag.log` for DDP jitter. |
| *Unknown palettes* warning in SETTINGS | A name in a phrase palette list doesn't exist. Fix the name, or create the palette in LED CONTROL. |

## AI

| Symptom | Fix |
|---|---|
| *Not initialized* | Paste a valid key in AI CONTROL and click **Initialize AI** |
| `analysis error: … model …` | The model ID in SETTINGS → AI models isn't available to your key. Pick another one. |
| Plans are slow | The fast model is used for plans. Keep it on a Haiku-class model. |

## Logs

| File | Contents |
|---|---|
| `djlights_YYYYMMDD_HHMMSS.log` | Full app log (DEBUG) for each run |
| `djlights_diag.log` | BLT ingest health, rotated at 5 MB |
| BLT console | Everything prefixed with `[VP]` comes from `vp_blt_code.clj` |

When you open an issue, attach the relevant part of these logs.
