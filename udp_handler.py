"""DDP UDP sender for WLED (port 4048).

Architecture: producer-consumer, one persistent sender thread per IP.

Optimalisaties (drie prompts gecombineerd):

  Prompt 1 — WiFi-friendly pakkettering
  • _MAX_PAYLOAD = 1440: ruim onder de ESP32-UDP-buffer limiet, vermijdt
    IP-fragmentatie op WiFi (MSDU aggregation).
  • Header caching: type (0x01) en id (0x01) worden eenmalig in buf[2:4]
    gezet; per pakket worden alleen de veranderende velden (flags, seq,
    offset, len) bijgewerkt via directe byte-writes + struct.pack_into op
    de 6 veranderende bytes — geen volledige header-repack elke iteratie.

  Prompt 2 — Delta-compressie
  • Per IP wordt het laatste verzonden frame bijgehouden (_last_pixels).
  • Identieke frames worden overgeslagen (np.array_equal, O(n) vectorized).
  • Heartbeat: na 2 s wordt alsnog een volledig frame gestuurd zodat WLED
    niet kan de-synchen na een tijdelijke drop.
  • Partial send: als alleen een aaneengesloten bereik van LEDs veranderd
    is én dat bereik < 75 % van de strip beslaat, wordt uitsluitend dat
    bereik verstuurd via de DDP byte-offset parameter.

  Prompt 3 — Zero-copy datapad
  • De render loop geeft de numpy array rechtstreeks aan de queue; geen
    tobytes()-call op de hot path.
  • De worker thread kopieert de pixeldata via np.frombuffer + np.copyto
    naar _pixel_buf (pre-allocated bytearray) — geen tijdelijk bytes-object.
  • Op Unix (macOS/Linux) gebruikt sendmsg() scatter-gather I/O: header en
    pixeldata worden in één syscall verstuurd zonder ze samen te voegen in
    een derde buffer.
  • Jitter-diagnostiek: waarschuwing als de inter-frame tijd > 150 % van
    het doel-interval (standaard 44 fps).
"""
import ctypes
import ctypes.util
import logging
import os
import queue
import socket
import struct
import subprocess
import threading
import time

import numpy as np

_log = logging.getLogger("ddp")


# ─────────────────────────────────────────────────────────────────────────────
# macOS thread-prioriteit via pthread QoS API
# ─────────────────────────────────────────────────────────────────────────────

# QoS-klassen (macOS <sys/qos.h>)
_QOS_CLASS_USER_INTERACTIVE = 0x21   # hoogste prioriteit — animatie / audio
_QOS_CLASS_USER_INITIATED   = 0x19   # hoog — interactieve taken
_QOS_CLASS_DEFAULT          = 0x15

_pthread = None
try:
    _lib = ctypes.util.find_library("pthread")
    if _lib:
        _pthread = ctypes.CDLL(_lib)
        # int pthread_set_qos_class_self_np(qos_class_t, int relative_priority)
        _pthread.pthread_set_qos_class_self_np.restype  = ctypes.c_int
        _pthread.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint32, ctypes.c_int]
except Exception:
    _pthread = None


def _boost_thread_qos(qos_class: int = _QOS_CLASS_USER_INTERACTIVE) -> bool:
    """Verhoog QoS van de *huidige* thread naar USER_INTERACTIVE.

    Moet worden aangeroepen vanuit de thread die je wilt boosten.
    Retourneert True bij succes, False als de API niet beschikbaar is.
    """
    if _pthread is None:
        return False
    try:
        rc = _pthread.pthread_set_qos_class_self_np(qos_class, 0)
        if rc == 0:
            _log.debug("Thread QoS → USER_INTERACTIVE (0x%02x)", qos_class)
            return True
        _log.warning("pthread_set_qos_class_self_np rc=%d", rc)
    except Exception as exc:
        _log.debug("QoS boost mislukt: %s", exc)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# macOS AWDL (AirDrop / AirPlay WiFi-interface) beheer
# ─────────────────────────────────────────────────────────────────────────────

def awdl_disable() -> bool:
    """Zet de AWDL-interface uit om WiFi-pingspikes te voorkomen.

    AWDL scant iedere ~20 s op andere Apple-apparaten en veroorzaakt
    tijdelijke latency-pieken op het WiFi-kanaal.  Vereist sudo of
    'network configuration' rechten; stelt geen eisen aan hardware.

    Retourneert True bij succes.
    """
    return _awdl_set(up=False)


def awdl_enable() -> bool:
    """Zet AWDL terug aan (herstel na awdl_disable)."""
    return _awdl_set(up=True)


def _awdl_set(up: bool) -> bool:
    state = "up" if up else "down"
    try:
        result = subprocess.run(
            ["sudo", "ifconfig", "awdl0", state],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            _log.info("AWDL interface awdl0 → %s", state)
            return True
        _log.warning("AWDL %s mislukt (rc=%d): %s",
                     state, result.returncode, result.stderr.strip())
    except FileNotFoundError:
        _log.warning("ifconfig niet gevonden — niet op macOS/Linux?")
    except subprocess.TimeoutExpired:
        _log.warning("AWDL %s timeout", state)
    except Exception as exc:
        _log.warning("AWDL %s fout: %s", state, exc)
    return False

import settings as _settings
DDP_PORT           = int(_settings.get("network.ddp_port", 4048))
_HDR_SIZE          = 10      # flags(1) seq(1) type(1) id(1) offset(4) len(2)
_MAX_PAYLOAD       = 1440    # 480 LEDs × 3 — WiFi-safe, ver onder 1500-byte MTU
_BUF_SIZE          = _HDR_SIZE + _MAX_PAYLOAD

_HEARTBEAT         = 2.0     # s: stuur volledig frame ook als niets veranderd is
_TARGET_FPS        = int(_settings.get("render.fps", 44))     # voor jitter-drempelberekening
_PARTIAL_THRESHOLD = 0.75    # partial send alleen als gewijzigd bereik < 75 % strip

_SENTINEL          = object()   # stoptoken voor sender-thread


# ─────────────────────────────────────────────────────────────────────────────
class _IPSender:
    """Één persistente thread + single-slot queue per IP-adres."""

    def __init__(self, ip: str):
        self._ip   = ip
        self._seq  = 0
        self._q    : queue.Queue = queue.Queue(maxsize=1)
        self._sock = self._make_sock()

        # ── Pre-allocated buffers ──────────────────────────────────────────
        self._hdr_buf   = bytearray(_HDR_SIZE)   # DDP header (10 B)
        self._pixel_buf = bytearray(0)            # groeit mee, krimpt nooit
        # Vul constante header-bytes eenmalig in — worden nooit overschreven
        self._hdr_buf[2] = 0x01   # type: RGB data
        self._hdr_buf[3] = 0x01   # id:   output 1

        # ── Delta-compressie state ─────────────────────────────────────────
        self._last_pixels : np.ndarray | None = None
        self._last_send_t : float = 0.0

        # ── Diagnostiek ───────────────────────────────────────────────────
        self._dropped     = 0
        self._sent        = 0
        self._skipped     = 0    # identieke frames niet verstuurd
        self._last_frame_t: float = 0.0

        # sendmsg beschikbaar op macOS/Linux — scatter-gather, geen memcopy
        self._use_sendmsg = hasattr(self._sock, "sendmsg")

        self._t = threading.Thread(target=self._loop,
                                   name=f"ddp-{ip}", daemon=True)
        self._t.start()

    # ── Publieke interface ─────────────────────────────────────────────────

    def enqueue(self, pixels: np.ndarray) -> None:
        """Non-blocking. Gooit het stale frame weg als de queue vol is."""
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
            self._dropped += 1

    def stop(self) -> None:
        self._q.put(_SENTINEL)
        self._t.join(timeout=2.0)
        try:
            self._sock.close()
        except Exception:
            pass

    @property
    def stats(self) -> dict:
        return {
            "sent":    self._sent,
            "dropped": self._dropped,
            "skipped": self._skipped,
        }

    # ── Privé ─────────────────────────────────────────────────────────────

    def _make_sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF
        except OSError:
            pass
        return s

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) % 256
        return self._seq

    def _loop(self) -> None:
        # Verhoog QoS van déze thread naar USER_INTERACTIVE zodat macOS GCD
        # hem niet "slaperig" maakt tijdens time.sleep() calls.
        _boost_thread_qos()

        ip     = self._ip
        hdr    = self._hdr_buf
        addr   = (ip, DDP_PORT)
        target = 1.0 / _TARGET_FPS

        while True:
            # ── Wacht op nieuw frame ──────────────────────────────────────
            try:
                pixels = self._q.get(timeout=1.0)
            except queue.Empty:
                continue

            if pixels is _SENTINEL:
                break

            # ── Jitter-diagnostiek ────────────────────────────────────────
            now = time.perf_counter()
            if self._last_frame_t > 0:
                gap = now - self._last_frame_t
                if gap > target * 1.5:
                    _log.warning(
                        "DDP %s jitter: %.1f ms (doel %.1f ms)",
                        ip, gap * 1000, target * 1000,
                    )
            self._last_frame_t = now

            # ── Delta-check: sla identieke frames over ────────────────────
            prev = self._last_pixels
            if (prev is not None
                    and pixels.shape == prev.shape
                    and np.array_equal(pixels, prev)):
                if now - self._last_send_t < _HEARTBEAT:
                    self._skipped += 1
                    continue
                # Heartbeat — val door naar verzenden

            # ── Bepaal te verzenden bereik (partial of volledig) ──────────
            byte_offset = 0
            send_pixels = pixels
            if prev is not None and pixels.shape == prev.shape:
                changed = np.any(pixels != prev, axis=1)   # (n,) bool
                idx     = np.where(changed)[0]
                if len(idx):
                    first_led, last_led = int(idx[0]), int(idx[-1]) + 1
                    if (last_led - first_led) < pixels.shape[0] * _PARTIAL_THRESHOLD:
                        send_pixels = pixels[first_led:last_led]
                        byte_offset = first_led * 3

            # ── Zero-copy: schrijf pixels naar pre-alloc pixel_buf ────────
            n_bytes = send_pixels.nbytes
            if len(self._pixel_buf) < n_bytes:
                self._pixel_buf = bytearray(n_bytes)

            # np.frombuffer maakt een numpy-view op de bytearray (geen alloc)
            np.copyto(
                np.frombuffer(self._pixel_buf, dtype=np.uint8, count=n_bytes),
                send_pixels.ravel(),
            )
            pix_mv = memoryview(self._pixel_buf)

            # ── Packetiseer en stuur ──────────────────────────────────────
            offset = 0
            seq    = self._next_seq()
            hdr[1] = seq   # seq is constant voor alle pakketten in dit frame

            while offset < n_bytes:
                chunk_end = min(offset + _MAX_PAYLOAD, n_bytes)
                chunk_len = chunk_end - offset
                remaining = n_bytes - chunk_end

                # Overschrijf alleen veranderende header-bytes
                hdr[0] = 0x41 if remaining == 0 else 0x01          # flags
                struct.pack_into(">IH", hdr, 4,
                                 byte_offset + offset, chunk_len)   # offset + len

                try:
                    if self._use_sendmsg:
                        # Scatter-gather: header + payload in één syscall,
                        # zonder ze samen te voegen in een derde buffer
                        self._sock.sendmsg(
                            [hdr, pix_mv[offset:chunk_end]],
                            [], 0, addr,
                        )
                    else:
                        # Fallback (Windows): combineer in hdr_buf uitbreiding
                        combined = bytes(hdr) + bytes(pix_mv[offset:chunk_end])
                        self._sock.sendto(combined, addr)
                except OSError:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = self._make_sock()
                    self._use_sendmsg = hasattr(self._sock, "sendmsg")
                    break

                if remaining > 0:
                    time.sleep(0.001)
                offset = chunk_end

            self._last_pixels = pixels
            self._last_send_t = now
            self._sent += 1


# ─────────────────────────────────────────────────────────────────────────────
class DDPSender:
    """Publieke API — signatuur ongewijzigd t.o.v. vorige versie."""

    def __init__(self):
        self._workers: dict[str, _IPSender] = {}

    def _worker(self, ip: str) -> _IPSender:
        if ip not in self._workers:
            self._workers[ip] = _IPSender(ip)
        return self._workers[ip]

    def send(self, pixels: np.ndarray, ip: str) -> None:
        """Encodeer en enqueue pixels voor IP — retourneert altijd direct."""
        if ip.startswith("127."):
            return
        # Garanteer C-contiguous uint8 — al correct in normale render-output
        if pixels.dtype != np.uint8 or not pixels.flags["C_CONTIGUOUS"]:
            pixels = np.ascontiguousarray(pixels, dtype=np.uint8)
        # Geef numpy array door — geen tobytes() op de hot path
        self._worker(ip).enqueue(pixels)

    def send_parallel(self, sends: list) -> None:
        """Enqueue frames voor alle IPs — retourneert altijd direct."""
        for px, ip in sends:
            self.send(px, ip)

    def dropped_frames(self) -> dict[str, int]:
        return {ip: w.stats["dropped"] for ip, w in self._workers.items()}

    def stats(self) -> dict[str, dict]:
        return {ip: w.stats for ip, w in self._workers.items()}

    def close(self) -> None:
        for w in self._workers.values():
            w.stop()
        self._workers.clear()
