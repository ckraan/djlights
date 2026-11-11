"""Persistent JSON storage for devices, setups, and palettes."""
import json, os, shutil, logging

log = logging.getLogger("config")

DEVICES_FILE  = os.path.join(os.path.dirname(__file__), "wled_devices.json")
PALETTES_FILE = os.path.join(os.path.dirname(__file__), "palettes.json")
SETUPS_FILE   = os.path.join(os.path.dirname(__file__), "setups.json")

DEFAULT_DEVICES = []

DEFAULT_PALETTES = [
    {"name": "Cyberpunk", "stops": [
        {"pos": 0.0, "r": 60,  "g": 0,   "b": 120},
        {"pos": 0.33,"r": 247,"g": 37,  "b": 133},
        {"pos": 0.66,"r": 6,  "g": 255, "b": 165},
        {"pos": 1.0, "r": 255,"g": 255, "b": 255},
    ]},
    {"name": "Fire", "stops": [
        {"pos": 0.0, "r": 0,  "g": 0,  "b": 0},
        {"pos": 0.3, "r": 200,"g": 0,  "b": 0},
        {"pos": 0.6, "r": 255,"g": 140,"b": 0},
        {"pos": 0.8, "r": 255,"g": 220,"b": 0},
        {"pos": 1.0, "r": 255,"g": 255,"b": 255},
    ]},
    {"name": "Ocean", "stops": [
        {"pos": 0.0, "r": 0,  "g": 20,  "b": 80},
        {"pos": 0.4, "r": 0,  "g": 150, "b": 200},
        {"pos": 0.7, "r": 0,  "g": 200, "b": 180},
        {"pos": 1.0, "r": 100,"g": 240, "b": 255},
    ]},
    {"name": "Sunset", "stops": [
        {"pos": 0.0, "r": 255,"g": 100,"b": 0},
        {"pos": 0.3, "r": 255,"g": 50, "b": 100},
        {"pos": 0.6, "r": 150,"g": 0,  "b": 180},
        {"pos": 1.0, "r": 20, "g": 0,  "b": 80},
    ]},
    {"name": "Neon", "stops": [
        {"pos": 0.0, "r": 0,  "g": 255,"b": 0},
        {"pos": 0.25,"r": 255,"g": 0,  "b": 255},
        {"pos": 0.5, "r": 0,  "g": 255,"b": 255},
        {"pos": 0.75,"r": 255,"g": 255,"b": 0},
        {"pos": 1.0, "r": 0,  "g": 255,"b": 0},
    ]},
    {"name": "Monochrome", "stops": [
        {"pos": 0.0, "r": 0,  "g": 0,  "b": 0},
        {"pos": 1.0, "r": 255,"g": 255,"b": 255},
    ]},
    {"name": "Rave", "stops": [
        {"pos": 0.0,  "r": 255,"g": 0,  "b": 0},
        {"pos": 0.33, "r": 0,  "g": 255,"b": 0},
        {"pos": 0.66, "r": 0,  "g": 0,  "b": 255},
        {"pos": 1.0,  "r": 255,"g": 0,  "b": 0},
    ]},
    {"name": "DeepHouse", "stops": [
        {"pos": 0.0,  "r": 255, "g": 107, "b": 0},
        {"pos": 0.5,  "r": 255, "g": 140, "b": 66},
        {"pos": 1.0,  "r": 224, "g": 122, "b": 95},
    ]},
    {"name": "Ice", "stops": [
        {"pos": 0.0,  "r": 30,  "g": 58,  "b": 138},
        {"pos": 0.5,  "r": 224, "g": 242, "b": 254},
        {"pos": 1.0,  "r": 186, "g": 230, "b": 253},
    ]},
    {"name": "BlueOnly", "stops": [
        {"pos": 0.0,  "r": 30,  "g": 58,  "b": 138},
        {"pos": 0.33, "r": 59,  "g": 130, "b": 246},
        {"pos": 0.66, "r": 96,  "g": 165, "b": 250},
        {"pos": 1.0,  "r": 191, "g": 219, "b": 254},
    ]},
    {"name": "Contrast", "stops": [
        {"pos": 0.0,  "r": 234, "g": 88,  "b": 12},
        {"pos": 1.0,  "r": 14,  "g": 165, "b": 233},
    ]},
]


def _load(path, default):
    if not os.path.exists(path):
        _save(path, default)
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        log.warning("Corrupt config %s — restoring default", path)
        shutil.copy(path, path + ".bak")
        _save(path, default)
        return default


def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_devices():  return _load(DEVICES_FILE,  DEFAULT_DEVICES)
def save_devices(d): _save(DEVICES_FILE, d)
def load_palettes():  return _load(PALETTES_FILE, DEFAULT_PALETTES)
def save_palettes(p): _save(PALETTES_FILE, p)


# ── Setup storage ─────────────────────────────────────────────────────────────

def _default_setups():
    """Build initial setups.json, migrating existing wled_devices.json → vault."""
    vault_devices = _load(DEVICES_FILE, DEFAULT_DEVICES)
    return {
        "active": "vault",
        "setups": {
            "vault": vault_devices,
            "cave": [
                {
                    "name": "Cave Glorb 1",
                    "ip": "192.168.1.16",
                    "led_count": 120,
                    "enabled": True,
                    "geometry": {"type": "globe", "led_count": 120},
                },
                {
                    "name": "Cave Glorb 2",
                    "ip": "192.168.2.212",
                    "led_count": 120,
                    "enabled": True,
                    "geometry": {"type": "globe", "led_count": 120},
                },
            ],
        },
    }


def load_setups():
    if not os.path.exists(SETUPS_FILE):
        data = _default_setups()
        _save(SETUPS_FILE, data)
        return data
    try:
        with open(SETUPS_FILE) as f:
            return json.load(f)
    except Exception:
        log.warning("Corrupt setups.json — restoring default")
        shutil.copy(SETUPS_FILE, SETUPS_FILE + ".bak")
        data = _default_setups()
        _save(SETUPS_FILE, data)
        return data


def save_setups(data):
    _save(SETUPS_FILE, data)
