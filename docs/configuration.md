# Configuration

← [Back to README](../README.md)

Everything you normally change can be set in the app. The settings are stored
as JSON files in the project folder. They are git-ignored, so your rig and your
API key never end up in the repo.

| What | Where in the app | File |
|---|---|---|
| WLED devices, geometry, setups | LED CONTROL → Devices / Setup | `setups.json`, `wled_devices.json` |
| Colour palettes | LED CONTROL → Palettes → editor | `palettes.json` |
| Anthropic API key | AI CONTROL | `ai_config.json` |
| Ports, FPS, AI models, phrase defaults | SETTINGS | `settings.json` |
| Window and UI preferences | (automatic) | `djlights_prefs.json` |

## Devices and setups

A **device** is one WLED controller:

```json
{
  "name": "Vault Strips",
  "ip": "192.168.2.212",
  "led_count": 1500,
  "enabled": true,
  "geometry": { "type": "snake", "segments": 5, "leds_per_segment": 300, "gap_px": 4 }
}
```

| Geometry | Use for | Fields |
|---|---|---|
| `linear` | One strip | `led_count` |
| `snake` | N strips wired back and forth into a 2D panel | `segments`, `leds_per_segment`, `gap_px` (preview spacing) |
| `globe` | LEDs spread over a sphere (Fibonacci layout) | `led_count` |

A **setup** is a named group of devices, for example one per venue or room.
Switch between them with the **SETUP** selector in LED CONTROL.
[`setups.example.json`](../setups.example.json) is a real two-room example:
a 5×300 "vault" panel with a back strip, and a "cave" with two globes.

> **Note:** the generative *vault* renderer assumes a 5×300 snake layout, and
> the *globe* renderer assumes 120 LEDs (`vault_renderer.py`,
> `globe_renderer.py`). The standard phrase effects work with any LED count.

## Palettes

Palettes are colour gradients made of stops (`pos` 0–1 plus RGB). Edit them in
**LED CONTROL → Palettes**. The phrase defaults below refer to palettes **by
name**.

## SETTINGS tab

Changes are saved to `settings.json` when you press **Save**. **Reset to
defaults** loads the built-in defaults, and you still need to press Save to
keep them.

### Network & rendering *(restart djlights to apply)*

| Setting | Default | Meaning |
|---|---|---|
| OSC port | `9000` | BLT → app: beats, phrases, status |
| UDP port | `9001` | BLT → app: track metadata JSON |
| BLT web server URL | `http://localhost:17081` | Fallback polling of BLT's `params.json` |
| DDP port | `4048` | App → WLED realtime pixels |
| Feedback web port | `5050` | Phone page for rating the lights during a set |
| Render FPS | `44` | Frames per second sent to the LEDs (10–120) |

When you save new OSC or UDP ports, the tab also updates `vp-osc-port` and
`vp-udp-port` in [`blt/vp_blt_code.clj`](../blt/vp_blt_code.clj). Reopen the
show in Beat Link Trigger afterwards.

### AI models *(applied immediately)*

| Setting | Default | Used for |
|---|---|---|
| Fast model | `claude-haiku-4-5` | Live decisions, per-track show plans, vault/globe plans |
| Smart model | `claude-sonnet-4-6` | *Learn from Set*: distilling rules after a session |

You can type any model ID the Anthropic API accepts.

### Phrase defaults *(applied immediately)*

These are used when the AI hasn't planned the current track, or when AI is off.

| Setting | Meaning |
|---|---|
| Brightness | 0–100 % per phrase type |
| Palettes | Comma-separated palette names. One is picked at random on each phrase change, never the same one twice in a row. |

If a palette name doesn't exist in the palette editor, saving shows a warning.
At runtime that name is skipped.

### `settings.json` format

```json
{
  "network": { "osc_port": 9000, "udp_port": 9001, "blt_url": "http://localhost:17081",
               "ddp_port": 4048, "feedback_port": 5050 },
  "render":  { "fps": 44 },
  "ai":      { "fast_model": "claude-haiku-4-5", "smart_model": "claude-sonnet-4-6" },
  "phrases": {
    "brightness":    { "intro": 0.35, "verse": 0.6, "up": 0.8, "chorus": 1.0,
                       "bridge": 0.55, "down": 0.45, "outro": 0.3 },
    "palette_pools": { "chorus": ["Fire", "Crimson", "Amber"] }
  }
}
```

Missing keys fall back to the defaults in [`settings.py`](../settings.py), so a
partial file is fine.

## Not configurable in the UI (yet)

| What | Where |
|---|---|
| Default direction and spacing per phrase | `wled_ctrl.py` (`PHRASE_DIRECTION_DEFAULT`, `PHRASE_SPACING_DEFAULT`) |
| Vault (5×300) and globe (120) renderer dimensions | `vault_renderer.py`, `globe_renderer.py` |
| AI prompts | `prompts/*.md` |
| DJM-A9 MIDI deck weighting | `mixer_midi.py` / `mixer_midi_tab.py` (the tab exists but is not mounted) |
