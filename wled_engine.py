"""wled_engine.py — High-performance animatie + DDP engine voor 1500 LEDs.

Combineert AnimationEngine (animation_engine.py) en DDPSender (udp_handler.py)
in één zelfstandige module.

Pipeline per frame:
    render()  →  float32 canvas
              →  Global Brightness Limiter   (stroom-cap)
              →  Gamma 2.2 LUT               (perceptueel lineair)
              →  uint8 cast                  (één allocatie)
              →  DDPSender                   (segmentatie, push-flag, queue)

DDP-segmentatie:
    Pakketten 1…n-1  →  flags = 0x01  (data, geen flush)
    Laatste pakket   →  flags = 0x41  (data + PUSH — triggert WLED render)

FPS-monitor:
    Iedere seconde wordt geprint:
        [FPS] render=44  sent=44  dropped=0  skipped=12  budget=22.7ms  actual=17.3ms
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from collections import deque

import numpy as np
import opensimplex

_log = logging.getLogger("wled_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Constanten
# ─────────────────────────────────────────────────────────────────────────────

DDP_PORT     = 4048
_HDR_SIZE    = 10        # flags(1) seq(1) type(1) id(1) offset(4) len(2)
_MAX_PAYLOAD = 1440      # 480 LEDs × 3 — WiFi-veilig, ver onder 1500-byte MTU

GAMMA        = 2.2
LUT_SIZE     = 4096      # 4096 stappen → geen banding bij donkere tinten
TARGET_FPS   = 44

# Stroom-limiet: som van alle LED-kanalen mag niet groter zijn dan dit getal
# als fractie van het theoretisch maximum (n × 3 × 255).
# 0.25 ≈ gemiddeld 25 % duty-cycle → ~3 A bij 60 LEDs/m op 5 V
POWER_BUDGET = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# 1. Gamma LUT
# ─────────────────────────────────────────────────────────────────────────────

def _build_gamma_lut(gamma: float = GAMMA, size: int = LUT_SIZE) -> np.ndarray:
    """float32 LUT: lineaire intensiteit [0,1] → gecorrigeerde stuurwaarde [0,1]."""
    x = np.linspace(0.0, 1.0, size, dtype=np.float32)
    return np.power(x, 1.0 / gamma, dtype=np.float32)


_GAMMA_LUT = _build_gamma_lut()   # module-niveau singleton


def apply_gamma(pixels: np.ndarray) -> np.ndarray:
    """Pas gamma-LUT toe op (n, 3) float32 array. Retourneert nieuwe float32 array."""
    idx = np.clip((pixels * (LUT_SIZE - 1)).astype(np.int32), 0, LUT_SIZE - 1)
    return _GAMMA_LUT[idx]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Global Brightness Limiter (stroom-cap)
# ─────────────────────────────────────────────────────────────────────────────

def apply_power_limit(pixels: np.ndarray,
                      budget: float = POWER_BUDGET) -> np.ndarray:
    """Schaal het frame omlaag als het totale vermogen het budget overschrijdt.

    Werkt op float32 [0, 1] voor gamma-correctie.  De schaalfactor is
    de wortel van de verhouding zodat de helderheid perceptueel lineair
    afneemt (niet lineair in vermogen).

    Parameters
    ----------
    pixels : (n, 3) float32 in [0, 1]
    budget : fractie van max vermogen  (bijv. 0.25 = 25 %)
    """
    total_max = pixels.shape[0] * 3.0        # n × 3 kanalen op max 1.0
    total_now = float(np.sum(pixels))
    limit     = total_max * budget
    if total_now <= limit:
        return pixels                        # binnen budget — geen aanpassing
    scale = np.sqrt(limit / total_now)       # perceptueel-lineaire schaling
    return pixels * np.float32(scale)


# ─────────────────────────────────────────────────────────────────────────────
# 3. DDP sender — persistent thread per IP, single-slot queue
# ─────────────────────────────────────────────────────────────────────────────

_STOP = object()   # stoptoken


class _IPSender:
    """Één persistente sender-thread + single-slot queue per IP-adres."""

    def __init__(self, ip: str):
        self._ip          = ip
        self._seq         = 0
        self._q           : queue.Queue = queue.Queue(maxsize=1)
        self._sock        = self._make_sock()
        self._use_sendmsg = hasattr(self._sock, "sendmsg")

        # Pre-allocated buffers — geen heap-allocatie in de hot path
        self._hdr_buf   = bytearray(_HDR_SIZE)
        self._pixel_buf = bytearray(0)    # groeit mee, krimpt nooit
        self._hdr_buf[2] = 0x01           # type: RGB (constant)
        self._hdr_buf[3] = 0x01           # id:   output 1 (constant)

        # Delta-compressie
        self._last_pixels : np.ndarray | None = None
        self._last_send_t : float = 0.0
        self._heartbeat   : float = 2.0   # s: resend ook bij geen wijziging

        # Statistieken (gelezen door FPS-monitor)
        self.dropped  = 0
        self.sent     = 0
        self.skipped  = 0

        self._t = threading.Thread(target=self._loop,
                                   name=f"ddp-{ip}", daemon=True)
        self._t.start()

    # ── Publiek ───────────────────────────────────────────────────────────

    def enqueue(self, pixels: np.ndarray) -> None:
        """Non-blocking. Stale frame wordt weggegooid als queue vol is."""
        try:
            self._q.put_nowait(pixels)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(pixels)
            except queue.Full:
                pass
            self.dropped += 1

    def stop(self) -> None:
        self._q.put(_STOP)
        self._t.join(timeout=2.0)
        try:
            self._sock.close()
        except Exception:
            pass

    # ── Privé ─────────────────────────────────────────────────────────────

    def _make_sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)   # DSCP EF
        except OSError:
            pass
        return s

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) % 256
        return self._seq

    def _loop(self) -> None:
        ip   = self._ip
        hdr  = self._hdr_buf
        addr = (ip, DDP_PORT)

        while True:
            try:
                pixels = self._q.get(timeout=1.0)
            except queue.Empty:
                continue

            if pixels is _STOP:
                break

            now  = time.perf_counter()
            prev = self._last_pixels

            # Delta-check: sla identieke frames over (met heartbeat)
            if (prev is not None
                    and pixels.shape == prev.shape
                    and np.array_equal(pixels, prev)
                    and now - self._last_send_t < self._heartbeat):
                self.skipped += 1
                continue

            # Zero-copy: schrijf pixels naar pre-alloc pixel_buf
            n_bytes = pixels.nbytes
            if len(self._pixel_buf) < n_bytes:
                self._pixel_buf = bytearray(n_bytes)
            np.copyto(
                np.frombuffer(self._pixel_buf, dtype=np.uint8, count=n_bytes),
                pixels.ravel(),
            )
            pix_mv = memoryview(self._pixel_buf)

            # ── DDP segmentatie ───────────────────────────────────────────
            # Pakketten 1..n-1  →  flags = 0x01  (data, geen flush)
            # Laatste pakket    →  flags = 0x41  (data + PUSH)
            offset = 0
            seq    = self._next_seq()
            hdr[1] = seq   # seq constant voor alle pakketten in dit frame

            while offset < n_bytes:
                chunk_end = min(offset + _MAX_PAYLOAD, n_bytes)
                chunk_len = chunk_end - offset
                remaining = n_bytes - chunk_end

                hdr[0] = 0x41 if remaining == 0 else 0x01   # PUSH alleen op laatste
                struct.pack_into(">IH", hdr, 4, offset, chunk_len)

                try:
                    if self._use_sendmsg:
                        # Scatter-gather: geen memcopy van header + data
                        self._sock.sendmsg(
                            [hdr, pix_mv[offset:chunk_end]],
                            [], 0, addr,
                        )
                    else:
                        self._sock.sendto(
                            bytes(hdr) + bytes(pix_mv[offset:chunk_end]), addr
                        )
                except OSError:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock     = self._make_sock()
                    self._use_sendmsg = hasattr(self._sock, "sendmsg")
                    break

                if remaining > 0:
                    time.sleep(0.001)   # 1 ms gap tussen pakketten
                offset = chunk_end

            self._last_pixels = pixels
            self._last_send_t = now
            self.sent += 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Noise & palette helpers  (intern, niet extern API)
# ─────────────────────────────────────────────────────────────────────────────

def _noise1d(n: int, t: float, spatial: float, speed: float,
             seed: int = 0) -> np.ndarray:
    opensimplex.seed(seed)
    xs  = np.arange(n, dtype=np.float64) * spatial
    raw = np.fromiter(
        (opensimplex.noise2(x, t * speed) for x in xs),
        dtype=np.float32, count=n,
    )
    return (raw + 1.0) * 0.5   # [-1,1] → [0,1]


def _make_palette(stops: list) -> np.ndarray:
    """Interpoleer kleurstops → (256, 3) float32 LUT."""
    stops = sorted(stops, key=lambda s: s[0])
    lut   = np.zeros((256, 3), dtype=np.float32)
    xs    = np.linspace(0.0, 1.0, 256)
    for ch in range(3):
        px = np.array([s[0]          for s in stops], dtype=np.float32)
        py = np.array([s[1][ch]/255. for s in stops], dtype=np.float32)
        lut[:, ch] = np.interp(xs, px, py).astype(np.float32)
    return lut


def _sample_palette(values: np.ndarray, lut: np.ndarray) -> np.ndarray:
    idx = np.clip((values * 255).astype(np.int32), 0, 255)
    return lut[idx]


# ─────────────────────────────────────────────────────────────────────────────
# 5. WledEngine — publieke API
# ─────────────────────────────────────────────────────────────────────────────

BUILTIN_PALETTES = {
    "aurora": [
        (0.00, (  0,   5,  20)),
        (0.30, (  0, 180,  80)),
        (0.55, ( 20, 100, 200)),
        (0.80, (160,   0, 200)),
        (1.00, (  0,   5,  20)),
    ],
    "fire":  [
        (0.00, (  0,   0,   0)),
        (0.30, (200,   0,   0)),
        (0.60, (255, 140,   0)),
        (0.85, (255, 220,   0)),
        (1.00, (255, 255, 200)),
    ],
    "ocean": [
        (0.00, (  0,  10,  60)),
        (0.40, (  0, 120, 180)),
        (0.70, (  0, 190, 160)),
        (1.00, ( 80, 230, 255)),
    ],
    "neon":  [
        (0.00, (  0, 255,   0)),
        (0.25, (255,   0, 255)),
        (0.50, (  0, 255, 255)),
        (0.75, (255, 255,   0)),
        (1.00, (  0, 255,   0)),
    ],
}


class WledEngine:
    """High-performance animatie + DDP engine voor WLED.

    Gebruik
    -------
        engine = WledEngine(ip="192.168.2.212", n_leds=1500)
        engine.start()          # start render-loop en FPS-monitor
        # ... druk Ctrl-C om te stoppen
        engine.stop()
    """

    def __init__(
        self,
        ip           : str   = "192.168.2.212",
        n_leds       : int   = 1500,
        fps          : int   = TARGET_FPS,
        brightness   : float = 0.25,
        power_budget : float = POWER_BUDGET,
        palette      : str   = "aurora",
        gamma        : float = GAMMA,
    ):
        self.ip           = ip
        self.n            = n_leds
        self.fps          = fps
        self.brightness   = float(brightness)
        self.power_budget = float(power_budget)

        # Gamma LUT (module-niveau singleton hergebruikt tenzij andere gamma)
        self._gamma_lut = _build_gamma_lut(gamma) if gamma != GAMMA else _GAMMA_LUT

        # Palette
        self._palette_lut = _make_palette(
            BUILTIN_PALETTES.get(palette, BUILTIN_PALETTES["aurora"])
        )

        # DDP worker
        self._sender = _IPSender(ip)

        # Herbruikbare canvas
        self._canvas = np.zeros((n_leds, 3), dtype=np.float32)

        # FPS-monitor statistieken
        self._fps_rendered = deque(maxlen=fps * 2)   # timestamps render
        self._fps_sent     = deque(maxlen=fps * 2)   # timestamps DDP enqueue

        self._running   = False
        self._thread    : threading.Thread | None = None
        self._monitor_t : threading.Thread | None = None

    # ── Publiek ───────────────────────────────────────────────────────────

    def set_palette(self, name: str) -> None:
        stops = BUILTIN_PALETTES.get(name, BUILTIN_PALETTES["aurora"])
        self._palette_lut = _make_palette(stops)

    def render_frame(self, t: float) -> np.ndarray:
        """Bereken één frame. Retourneert (n, 3) uint8.

        Roep dit aan vanuit je eigen loop als je geen ingebouwde loop wilt.
        """
        canvas = self._canvas
        canvas[:] = 0.0

        # Laag 1: simplex-ruis als kleurbase
        noise = _noise1d(self.n, t, spatial=0.005, speed=0.25)
        canvas += _sample_palette(noise, self._palette_lut)

        # Laag 2: intensiteitsmasker (tweede noise-octaaf)
        intensity = _noise1d(self.n, t, spatial=0.012, speed=0.5, seed=10)
        canvas   *= (intensity ** 1.4)[:, np.newaxis]

        # Gebruikershelderheid toepassen
        canvas *= np.float32(self.brightness)

        # Global Brightness Limiter — schaal bij stroomoverschrijding
        limited = apply_power_limit(canvas, self.power_budget)

        # Gamma-correctie via LUT
        idx       = np.clip(
            (np.clip(limited, 0.0, 1.0) * (LUT_SIZE - 1)).astype(np.int32),
            0, LUT_SIZE - 1,
        )
        corrected = self._gamma_lut[idx]

        # Enige uint8-cast in de pipeline
        return (corrected * 255.0).astype(np.uint8)

    def start(self) -> None:
        """Start ingebouwde render-loop en FPS-monitor in achtergrond-threads."""
        self._running   = True
        self._start_t   = time.perf_counter()
        self._thread    = threading.Thread(target=self._render_loop,
                                           name="render", daemon=True)
        self._monitor_t = threading.Thread(target=self._fps_monitor,
                                           name="fps-monitor", daemon=True)
        self._thread.start()
        self._monitor_t.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        self._sender.stop()

    # ── Privé ─────────────────────────────────────────────────────────────

    def _render_loop(self) -> None:
        interval   = 1.0 / self.fps
        next_frame = time.perf_counter()

        while self._running:
            now = time.perf_counter()
            if now < next_frame:
                time.sleep(next_frame - now)
            next_frame += interval   # cumulatieve optelling compenseert drift

            t     = time.perf_counter() - self._start_t
            frame = self.render_frame(t)

            self._fps_rendered.append(time.perf_counter())
            self._sender.enqueue(frame)
            self._fps_sent.append(time.perf_counter())

    def _fps_monitor(self) -> None:
        """Print iedere seconde render- vs. DDP-statistieken."""
        budget_ms = 1000.0 / self.fps
        prev_sent    = 0
        prev_dropped = 0
        prev_skipped = 0

        while self._running:
            time.sleep(1.0)

            now   = time.perf_counter()
            cutoff = now - 1.0

            # Frames rendered in het afgelopen tweede
            rendered = sum(1 for ts in self._fps_rendered if ts >= cutoff)

            # Frames daadwerkelijk verzonden (delta op teller)
            sent_now    = self._sender.sent
            dropped_now = self._sender.dropped
            skipped_now = self._sender.skipped

            sent_delta    = sent_now    - prev_sent
            dropped_delta = dropped_now - prev_dropped
            skipped_delta = skipped_now - prev_skipped

            prev_sent    = sent_now
            prev_dropped = dropped_now
            prev_skipped = skipped_now

            # Gemiddelde render-tijd schatten uit recente timestamps
            recent = [ts for ts in self._fps_rendered if ts >= cutoff]
            if len(recent) > 1:
                avg_interval_ms = (recent[-1] - recent[0]) / (len(recent) - 1) * 1000
            else:
                avg_interval_ms = 0.0

            print(
                f"[FPS] render={rendered:3d}  sent={sent_delta:3d}"
                f"  dropped={dropped_delta}  skipped={skipped_delta}"
                f"  budget={budget_ms:.1f}ms  actual={avg_interval_ms:.1f}ms"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark / demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="WledEngine benchmark / live run")
    parser.add_argument("--ip",      default="127.0.0.1",  help="WLED IP (127.0.0.1 = benchmark)")
    parser.add_argument("--leds",    type=int, default=1500)
    parser.add_argument("--fps",     type=int, default=44)
    parser.add_argument("--palette", default="aurora")
    parser.add_argument("--bench",   type=int, default=0,
                        help="Benchmark N frames en exit (0 = live run)")
    args = parser.parse_args()

    if args.bench:
        # Pure render-benchmark zonder netwerk
        engine = WledEngine(ip="127.0.0.1", n_leds=args.leds,
                            fps=args.fps, palette=args.palette)
        t0 = time.perf_counter()
        for i in range(args.bench):
            engine.render_frame(i / args.fps)
        elapsed = time.perf_counter() - t0
        ms      = elapsed / args.bench * 1000
        print(f"Benchmark {args.bench} frames × {args.leds} LEDs")
        print(f"  per frame : {ms:.2f} ms  (budget {1000/args.fps:.1f} ms)")
        print(f"  max FPS   : {1000/ms:.0f}")
    else:
        engine = WledEngine(ip=args.ip, n_leds=args.leds,
                            fps=args.fps, palette=args.palette)
        print(f"Starten → {args.ip}  {args.leds} LEDs  {args.fps} fps")
        print("Ctrl-C om te stoppen\n")
        engine.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            engine.stop()
