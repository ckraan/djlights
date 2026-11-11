"""
djlights — WLED DDP output
Sends pixel data to WLED via DDP protocol (UDP port 4048).
"""
import socket
import struct
import time
import numpy as np

WLED_IP   = "192.168.1.16"
DDP_PORT  = 4048
LED_COUNT = 1500
DSCP_EF   = 0xB8

_HDR_FMT      = ">BBBBIH"
_HDR_LEN      = struct.calcsize(_HDR_FMT)
_MAX_PAYLOAD  = 480   # bytes per packet = 160 LEDs × 3

_seq  = 0
_sock = None


def _next_seq():
    global _seq
    _seq = (_seq + 1) % 256
    return _seq


def _get_sock():
    global _sock
    if _sock is None:
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_EF)
        except OSError:
            pass
    return _sock


def send_pixels(pixels: np.ndarray, ip: str = WLED_IP) -> None:
    raw    = pixels.astype(np.uint8).tobytes()
    sock   = _get_sock()
    total  = len(raw)
    offset = 0
    seq    = _next_seq()

    while offset < total:
        chunk     = raw[offset:offset + _MAX_PAYLOAD]
        remaining = total - (offset + len(chunk))
        flags     = 0x41 if remaining == 0 else 0x01
        hdr       = struct.pack(_HDR_FMT, flags, seq, 0x01, 0x01, offset, len(chunk))
        try:
            sock.sendto(hdr + chunk, (ip, DDP_PORT))
        except OSError:
            return   # WLED not reachable — skip silently
        if remaining > 0:
            time.sleep(0.001)
        offset += len(chunk)


def black(count: int = LED_COUNT) -> np.ndarray:
    return np.zeros((count, 3), dtype=np.uint8)


def solid(r: int, g: int, b: int, count: int = LED_COUNT) -> np.ndarray:
    px = np.zeros((count, 3), dtype=np.uint8)
    px[:] = (r, g, b)
    return px
