"""
settings.py — app-wide settings stored in settings.json (next to this file).

Edited from the SETTINGS tab in the app (settings_tab.py). Missing keys fall
back to DEFAULTS, so an old or partial settings.json keeps working.

Usage:
    import settings
    settings.get("network.osc_port")      # → 9000
    settings.set("render.fps", 60); settings.save()
"""
import copy
import json
import logging
import os

log = logging.getLogger("settings")

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

PHRASES = ["intro", "verse", "up", "chorus", "bridge", "down", "outro"]

DEFAULTS = {
    "network": {
        # Must match the ports in blt/vp_blt_code.clj (vp-osc-port / vp-udp-port)
        "osc_port": 9000,                       # BLT → app, OSC beat/status/phrase
        "udp_port": 9001,                       # BLT → app, track metadata JSON
        "blt_url": "http://localhost:17081",    # BLT web server (fallback polling)
        "ddp_port": 4048,                       # app → WLED realtime pixels
        "feedback_port": 5050,                  # phone feedback web page
    },
    "render": {
        "fps": 44,                              # frames per second sent to WLED
    },
    "ai": {
        "fast_model": "claude-haiku-4-5",       # live decisions + per-track plans
        "smart_model": "claude-sonnet-4-6",     # post-session learning
    },
    "phrases": {
        # Brightness 0.0–1.0 per phrase when the AI hasn't decided
        "brightness": {
            "intro": 0.35, "verse": 0.60, "up": 0.80, "chorus": 1.00,
            "bridge": 0.55, "down": 0.45, "outro": 0.30,
        },
        # Palette names (from the palette editor) to pick from per phrase
        "palette_pools": {
            "intro":  ["DeepBlue", "Ocean", "Violet", "Purple", "BlueOnly", "Monochrome"],
            "verse":  ["Neon", "DeepHouse", "Ocean", "Sunset", "beam", "bro", "Violet", "Purple", "Pink"],
            "up":     ["Sunset", "Neon", "Cyberpunk", "Contrast", "Amber", "Fire", "Orange", "bro"],
            "chorus": ["Fire", "Cyberpunk", "Crimson", "DarkRed", "Contrast", "Neon", "Amber", "Orange", "Sunset"],
            "down":   ["DeepBlue", "Ocean", "Violet", "Purple", "Velvet", "BlueOnly", "DeepHouse"],
            "bridge": ["Purple", "Pink", "Velvet", "Cyberpunk", "DarkRed", "Monochrome", "Crimson"],
            "outro":  ["DeepBlue", "BlueOnly", "Monochrome", "Violet", "Purple", "Ocean", "Velvet"],
        },
    },
}

# Keys whose change only takes effect after restarting the app
RESTART_KEYS = {
    "network.osc_port", "network.udp_port", "network.blt_url",
    "network.ddp_port", "network.feedback_port", "render.fps",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return _merge(DEFAULTS, json.load(f))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except Exception as e:
        log.warning("settings.json unreadable (%s) — using defaults", e)
        return copy.deepcopy(DEFAULTS)


_data = _load()


def get(path: str, default=None):
    node = _data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set(path: str, value) -> None:  # noqa: A001 — mirrors get()
    parts = path.split(".")
    node = _data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def save() -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(_data, f, indent=2)


def reset_to_defaults() -> None:
    global _data
    _data = copy.deepcopy(DEFAULTS)


def all_settings() -> dict:
    return copy.deepcopy(_data)
