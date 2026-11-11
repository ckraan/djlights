"""WLED device management — add, remove, ping, enable/disable, setup switching."""
import socket
import threading
from config_manager import load_devices, save_devices, load_setups, save_setups
from geometry import Geometry, GlobeGeometry


class Device:
    def __init__(self, name, ip, led_count=300, enabled=True, geometry=None):
        self.name      = name
        self.ip        = ip
        self.led_count = int(led_count)
        self.enabled   = enabled
        self.online    = None          # None=unknown, True=up, False=down
        self.geometry  = geometry or {"type": "linear", "led_count": int(led_count)}

    def to_dict(self):
        return {"name": self.name, "ip": self.ip,
                "led_count": self.led_count, "enabled": self.enabled,
                "geometry": self.geometry}

    @property
    def led_order(self):
        """For globe devices: permutation array mapping effect position → physical LED.
        None for all other geometry types. Cached on first access."""
        if not hasattr(self, "_led_order_cache"):
            g = Geometry.from_dict(self.geometry)
            self._led_order_cache = g.led_order() if isinstance(g, GlobeGeometry) else None
        return self._led_order_cache

    @property
    def ring_render_size(self):
        """For grid globe: (ring_count, ring_size). None otherwise. Cached."""
        if not hasattr(self, "_ring_render_cache"):
            g = Geometry.from_dict(self.geometry)
            self._ring_render_cache = g.ring_render_size() if isinstance(g, GlobeGeometry) else None
        return self._ring_render_cache

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["ip"],
                   d.get("led_count", 300),
                   d.get("enabled", True),
                   d.get("geometry", None))


class DeviceManager:
    def __init__(self):
        self._lock        = threading.Lock()
        self._setups_data = load_setups()
        active            = self._setups_data.get("active", "vault")
        self._active_setup = active
        self.devices = [Device.from_dict(d)
                        for d in self._setups_data["setups"].get(active, [])]

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self):
        with self._lock:
            self._setups_data["setups"][self._active_setup] = [
                d.to_dict() for d in self.devices
            ]
        save_setups(self._setups_data)
        save_devices([d.to_dict() for d in self.devices])   # backward-compat

    # ── Setup management ──────────────────────────────────────────────────────

    @property
    def active_setup(self):
        return self._active_setup

    def setup_names(self):
        return list(self._setups_data["setups"].keys())

    def switch_setup(self, name: str):
        """Save current device state then load the named setup."""
        with self._lock:
            self._setups_data["setups"][self._active_setup] = [
                d.to_dict() for d in self.devices
            ]
            self._active_setup            = name
            self._setups_data["active"]   = name
            self.devices = [Device.from_dict(d)
                            for d in self._setups_data["setups"].get(name, [])]
        save_setups(self._setups_data)

    def add_setup(self, name: str):
        if name not in self._setups_data["setups"]:
            self._setups_data["setups"][name] = []
            save_setups(self._setups_data)

    def delete_setup(self, name: str):
        setups = self._setups_data["setups"]
        if name not in setups or len(setups) <= 1:
            return
        del setups[name]
        if self._active_setup == name:
            first = next(iter(setups))
            self._active_setup          = first
            self._setups_data["active"] = first
            with self._lock:
                self.devices = [Device.from_dict(d) for d in setups[first]]
        save_setups(self._setups_data)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add(self, name, ip, led_count=300):
        with self._lock:
            self.devices.append(Device(name, ip, int(led_count)))
            self.save()

    def remove(self, idx):
        with self._lock:
            self.devices.pop(idx)
            self.save()

    def update(self, idx, name, ip, led_count):
        with self._lock:
            d = self.devices[idx]
            d.name      = name
            d.ip        = ip
            d.led_count = int(led_count)
            self.save()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def enabled_devices(self):
        with self._lock:
            return [d for d in self.devices if d.enabled]

    def ping(self, device, callback=None):
        def _do():
            try:
                s = socket.create_connection((device.ip, 80), timeout=1.5)
                s.close()
                device.online = True
            except Exception:
                device.online = False
            if callback:
                callback(device)
        threading.Thread(target=_do, daemon=True).start()

    def ping_all(self, callback=None):
        for d in self.devices:
            self.ping(d, callback)
