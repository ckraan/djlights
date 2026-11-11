"""Advanced LED Render Engine — DDPCanvas persistent-buffer architecture.

Design contract
───────────────
  DDPCanvas     Persistent float32 frame buffer.  Lifecycle per tick:
                    canvas.decay(dt)          — exponential fade (motion blur)
                    entity.render(canvas)     — entities paint themselves
                    canvas.blur()             — optional 1D glow convolution
                    output = canvas.clamp()   — uint8 for DDP

  LEDEntity     Abstract base: update(dt) advances state; render(canvas) draws.

  Meteor        Head-only entity.  Trail is a side-effect of DDPCanvas.decay().
  Sparkle       Single point that fades out over its lifetime.
  Shockwave     Expanding Gaussian ring — non-particle, high visual impact.

  Particle      State: pos, vel, accel, mass, color, lifespan, age.
  ParticleEmitter  Vectorised NumPy batch physics — handles 200+ particles at 60 fps.
  Explosion     ParticleEmitter.trigger() bursts N particles with random velocities.
  Firework      Rising head that spawns an Explosion when its lifespan expires.
  GlitterEmitter  Stationary sparkle burst seeded once per trigger().

  AdvancedPattern   Abstract: seed() spawns entities into the shared entity list.
  AdvancedRenderer  Frame dispatcher — decay → seed → update → render → blur → clamp.
"""

from __future__ import annotations

import math
from abc    import ABC, abstractmethod
from enum   import Enum, auto
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
#  Blend modes
# ─────────────────────────────────────────────────────────────────────────────

class BlendMode(Enum):
    ADD    = auto()   # additive — naturally clamps at 255
    SCREEN = auto()   # photographic screen — prevents harsh over-exposure
    MAX    = auto()   # take brighter value — good for overlapping glows


# ─────────────────────────────────────────────────────────────────────────────
#  EngineConfig
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EngineConfig:
    n:          int
    decay_rate: float = 0.82     # per virtual-30-fps frame; time-corrected in decay()
    tone_map:   str   = "white_hot"  # "white_hot" | "reinhard" | "clip"
    tone_white: float = 512.0    # luminance that maps to pure white (used by tone mappers)


# ─────────────────────────────────────────────────────────────────────────────
#  Tone mapping functions
# ─────────────────────────────────────────────────────────────────────────────

def _tonemap_white_hot(buf: np.ndarray, white: float = 512.0) -> np.ndarray:
    """White-hot sigmoid tone mapper.

    The float32 buffer is allowed to accumulate values well above 255 during
    the Add phase.  This function maps them back to 0-255 for DDP output with
    a physically motivated compression curve:

        L ≤ 255           → pixel is unchanged  (linear passthrough)
        255 < L < white   → channels are lifted toward 255 via smoothstep
        L ≥ white         → pixel becomes pure white  [255, 255, 255]

    where L = max(R, G, B) per pixel.

    The smoothstep curve  s(t) = 3t² − 2t³  gives an S-shaped transition with
    zero slope at both endpoints — no hard knee and no abrupt colour jump.

    Visual result: a hot white core in overlapping explosions/meteors that
    radiates outward into fully saturated colour at normal brightness levels.

    Parameters
    ----------
    buf   : float32 (n, 3)  — unbounded HDR buffer
    white : luminance level that maps to pure white (default 512 = 2× overdrive)
    """
    x = buf.clip(min=0.0)

    # Per-pixel luminance proxy: max channel value
    L = x.max(axis=1, keepdims=True)                           # (n, 1)

    # Normalise excess above 255 into [0, 1] with clamping
    span  = max(white - 255.0, 1.0)
    t     = np.clip((L - 255.0) / span, 0.0, 1.0)             # (n, 1)

    # Smoothstep S-curve: 3t² − 2t³  (zero derivative at t=0 and t=1)
    excess = t * t * (3.0 - 2.0 * t)                          # (n, 1)

    # Lift each channel toward 255: out = c*(1−excess) + 255*excess
    out = x * (1.0 - excess) + 255.0 * excess                 # (n, 3)
    return out.clip(0.0, 255.0).astype(np.uint8)


def _tonemap_reinhard(buf: np.ndarray, white: float = 512.0) -> np.ndarray:
    """Extended Reinhard per-channel tone mapper.

    Applies  f(x) = x·(1 + x/W²) / (1 + x)  independently to each R, G, B
    channel (normalised so 255 = 1).  Preserves hue at low values; compresses
    highlights; asymptotically approaches white.  Does not produce the white-hot
    desaturation effect — use white_hot for that.

    Parameters
    ----------
    white : value in [0, ∞] that maps to ≈255 on output
    """
    x = buf.clip(min=0.0) / 255.0          # normalise: 255 → 1
    W = white / 255.0
    out = x * (1.0 + x / (W * W)) / (1.0 + x)
    return (out * 255.0).clip(0.0, 255.0).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
#  DDPCanvas — the persistent frame buffer
# ─────────────────────────────────────────────────────────────────────────────

class DDPCanvas:
    """Persistent float32 frame buffer shared by all entities in one render tick.

    Frame lifecycle (call once per tick, in order):
        canvas.decay(dt)          → exponential fade for motion-blur trails
        entity.render(canvas)     → each entity paints itself
        canvas.blur()             → optional 1D softening kernel
        output = canvas.clamp()   → safe uint8 for DDP transmission
    """

    # Default 1D blur kernel: [0.25, 0.5, 0.25]
    _BLUR_KERNEL = np.array([0.25, 0.5, 0.25], dtype=np.float32)

    def __init__(self, config: EngineConfig):
        self.config = config
        self.buffer = np.zeros((config.n, 3), dtype=np.float32)

    # ── Frame lifecycle ───────────────────────────────────────────────────

    def decay(self, dt: float = 1 / 30) -> None:
        """Vectorised per-pixel fade.

        Normalised to 30 fps so decay_rate=0.82 always produces the same visual
        trail regardless of the actual render rate.
        """
        self.buffer *= float(self.config.decay_rate) ** (dt * 30.0)

    def blur(self, kernel: Optional[np.ndarray] = None) -> None:
        """1D convolution across the buffer — turns sharp particles into soft glows.

        Default kernel [0.25, 0.5, 0.25] is a single-pass box blur.
        Call once per frame after all entities have rendered.
        """
        k = self._BLUR_KERNEL if kernel is None else kernel
        for ch in range(3):
            self.buffer[:, ch] = np.convolve(self.buffer[:, ch], k, mode="same")

    def clamp(self) -> np.ndarray:
        """Apply tone mapping and return uint8 (n, 3) array for DDP transmission.

        The buffer is allowed to hold values > 255 — tone mapping compresses
        the HDR range into 0-255 according to config.tone_map:

            "white_hot"  smoothstep desaturation toward white  (default)
            "reinhard"   per-channel extended Reinhard curve
            "clip"       hard clip — original behaviour, no HDR
        """
        tm = self.config.tone_map
        if tm == "white_hot":
            return _tonemap_white_hot(self.buffer, self.config.tone_white)
        if tm == "reinhard":
            return _tonemap_reinhard(self.buffer, self.config.tone_white)
        return np.clip(self.buffer, 0.0, 255.0).astype(np.uint8)  # "clip"

    def reset(self) -> None:
        self.buffer[:] = 0.0

    # ── Single-point drawing: sub-pixel linear interpolation ─────────────

    def draw(
        self,
        pos_f:      float,
        color:      tuple,          # (r, g, b) in 0–255 range
        blend:      BlendMode = BlendMode.ADD,
        brightness: float     = 1.0,
    ) -> None:
        """Draw one point at fractional position using linear interpolation.

        pos_f = 10.7 → pixel 10 gets 30 % and pixel 11 gets 70 % of the color.
        """
        n   = self.config.n
        i0  = int(pos_f) % n
        i1  = (i0 + 1) % n
        frc = pos_f - math.floor(pos_f)
        w0  = (1.0 - frc) * brightness
        w1  = frc          * brightness
        cr, cg, cb = float(color[0]), float(color[1]), float(color[2])
        for idx, w in ((i0, w0), (i1, w1)):
            c = np.array([cr * w, cg * w, cb * w], dtype=np.float32)
            self._blend(idx, c, blend)

    def _blend(self, idx: int, c: np.ndarray, mode: BlendMode) -> None:
        if mode is BlendMode.ADD:
            self.buffer[idx] += c
        elif mode is BlendMode.SCREEN:
            a = self.buffer[idx] / 255.0
            b = c / 255.0
            self.buffer[idx] = (1.0 - (1.0 - a) * (1.0 - b)) * 255.0
        elif mode is BlendMode.MAX:
            np.maximum(self.buffer[idx], c, out=self.buffer[idx])

    # ── Batch drawing: vectorised scatter-add for ParticleEmitter ────────

    def draw_batch(
        self,
        pos_arr:        np.ndarray,   # float32 (k,)  — fractional positions
        color_arr:      np.ndarray,   # float32 (k,3) — RGB 0-255
        brightness_arr: np.ndarray,   # float32 (k,)  — per-particle brightness
        blend:          BlendMode = BlendMode.ADD,
    ) -> None:
        """Vectorised sub-pixel compositing for many particles at once.

        Uses np.add.at for safe scatter-add without race conditions.
        O(k) instead of O(n), handles 200+ particles with minimal overhead.
        """
        if len(pos_arr) == 0:
            return
        n    = self.config.n
        i0   = pos_arr.astype(np.int32) % n
        i1   = (i0 + 1) % n
        frac = (pos_arr - np.floor(pos_arr)).astype(np.float32)
        w0   = ((1.0 - frac) * brightness_arr).astype(np.float32)  # (k,)
        w1   = (frac          * brightness_arr).astype(np.float32)  # (k,)

        contrib0 = color_arr * w0[:, None]   # (k, 3)
        contrib1 = color_arr * w1[:, None]   # (k, 3)

        if blend is BlendMode.ADD:
            np.add.at(self.buffer, i0, contrib0)
            np.add.at(self.buffer, i1, contrib1)
        elif blend is BlendMode.MAX:
            # np.maximum.at — slower but correct for MAX mode
            np.maximum.at(self.buffer, i0, contrib0)
            np.maximum.at(self.buffer, i1, contrib1)
        elif blend is BlendMode.SCREEN:
            # Screen is non-linear — fall back to per-particle loop
            for j in range(len(pos_arr)):
                self.draw(float(pos_arr[j]),
                          (color_arr[j, 0], color_arr[j, 1], color_arr[j, 2]),
                          BlendMode.SCREEN, float(brightness_arr[j]))

    # ── Gaussian blob drawing ─────────────────────────────────────────────

    def draw_gaussian(
        self,
        pos_f:      float,
        sigma:      float,
        color:      tuple,
        blend:      BlendMode = BlendMode.ADD,
        brightness: float     = 1.0,
    ) -> None:
        """Draw a soft Gaussian blob (useful for glows and shockwave rings)."""
        n   = self.config.n
        arr = np.arange(n, dtype=np.float32)
        dist = np.minimum(np.abs(arr - pos_f), n - np.abs(arr - pos_f))
        mask = np.exp(-(dist ** 2) / (2.0 * sigma ** 2)) * brightness
        cr, cg, cb = float(color[0]), float(color[1]), float(color[2])
        c = np.stack([mask * cr, mask * cg, mask * cb], axis=1).astype(np.float32)
        if blend is BlendMode.ADD:
            self.buffer += c
        elif blend is BlendMode.SCREEN:
            a = self.buffer / 255.0
            b = c / 255.0
            self.buffer[:] = (1.0 - (1.0 - a) * (1.0 - b)) * 255.0
        elif blend is BlendMode.MAX:
            np.maximum(self.buffer, c, out=self.buffer)


# ─────────────────────────────────────────────────────────────────────────────
#  LEDEntity — abstract base
# ─────────────────────────────────────────────────────────────────────────────

class LEDEntity(ABC):
    """Abstract entity.  update() advances physics; render() paints the canvas."""

    @abstractmethod
    def update(self, dt: float, bpm: float, n: int) -> bool:
        """Advance by dt seconds.  Return False when the entity has expired."""

    @abstractmethod
    def render(self, canvas: DDPCanvas) -> None:
        """Paint the entity's current state onto canvas."""


# ─────────────────────────────────────────────────────────────────────────────
#  Meteor — head-only entity (trail = canvas decay side-effect)
# ─────────────────────────────────────────────────────────────────────────────

class Meteor(LEDEntity):
    """A single bright head traveling along the strip.

    The trail is not stored here — it is a side-effect of DDPCanvas.decay().
    This means any decay_rate applied to the canvas automatically produces
    motion blur for every Meteor registered on it.
    """

    def __init__(
        self,
        pos:        float,
        vel:        float,          # LEDs per second; negative = reverse direction
        color:      tuple,
        brightness: float     = 1.0,
        blend:      BlendMode = BlendMode.ADD,
        bounce:     bool      = False,
    ):
        self.pos        = float(pos)
        self.vel        = float(vel)
        self.color      = color
        self.brightness = brightness
        self.blend      = blend
        self.bounce     = bounce
        self._alive     = True

    def update(self, dt: float, bpm: float, n: int) -> bool:
        self.pos += self.vel * dt
        if self.bounce:
            if self.pos < 0:
                self.pos = -self.pos
                self.vel = abs(self.vel)
            elif self.pos >= n:
                self.pos = 2.0 * n - self.pos - 2.0
                self.vel = -abs(self.vel)
        else:
            self.pos %= n
        return self._alive

    def render(self, canvas: DDPCanvas) -> None:
        canvas.draw(self.pos, self.color, self.blend, self.brightness)

    def kill(self) -> None:
        self._alive = False


# ─────────────────────────────────────────────────────────────────────────────
#  Sparkle — single point that fades over its lifetime
# ─────────────────────────────────────────────────────────────────────────────

class Sparkle(LEDEntity):
    def __init__(self, pos: float, color: tuple, lifetime: float, brightness: float = 1.0):
        self.pos        = float(pos)
        self.color      = color
        self._t         = 0.0
        self._lifetime  = max(0.001, lifetime)
        self.brightness = brightness

    def update(self, dt: float, bpm: float, n: int) -> bool:
        self._t += dt
        return self._t < self._lifetime

    def render(self, canvas: DDPCanvas) -> None:
        fade = max(0.0, 1.0 - self._t / self._lifetime) ** 2
        canvas.draw(self.pos, self.color, BlendMode.ADD, self.brightness * fade)


# ─────────────────────────────────────────────────────────────────────────────
#  Shockwave — expanding Gaussian ring (non-particle)
# ─────────────────────────────────────────────────────────────────────────────

class Shockwave(LEDEntity):
    """A ring of high-intensity light expanding from a centre point.

    Brightness is determined by a Gaussian function evaluated at the ring radius.
    Works symmetrically outward from centre in both directions.
    """

    def __init__(
        self,
        center:     float,
        color:      tuple,
        max_radius: float,
        speed:      float,          # LEDs per second
        sigma:      float  = 6.0,
        brightness: float  = 1.0,
    ):
        self._center     = float(center)
        self._radius     = 0.0
        self._color      = color
        self._max_radius = float(max_radius)
        self._speed      = float(speed)
        self._sigma      = sigma
        self._brightness = brightness

    def update(self, dt: float, bpm: float, n: int) -> bool:
        self._radius += self._speed * dt
        return self._radius < self._max_radius

    def render(self, canvas: DDPCanvas) -> None:
        fade = max(0.0, 1.0 - self._radius / self._max_radius) ** 0.7
        br   = self._brightness * fade
        canvas.draw_gaussian(self._center + self._radius, self._sigma,
                             self._color, BlendMode.ADD, br)
        canvas.draw_gaussian(self._center - self._radius, self._sigma,
                             self._color, BlendMode.ADD, br)


# ─────────────────────────────────────────────────────────────────────────────
#  Particle — individual physics state (used inside ParticleEmitter arrays)
# ─────────────────────────────────────────────────────────────────────────────

# Note: Particle is a logical descriptor; ParticleEmitter holds all particle
# state in NumPy arrays for vectorised physics — no Python objects per particle.


# ─────────────────────────────────────────────────────────────────────────────
#  ParticleEmitter — vectorised batch physics engine
# ─────────────────────────────────────────────────────────────────────────────

class ParticleEmitter(LEDEntity):
    """Holds all particle state in flat NumPy arrays.

    Physics (pos, vel, accel, drag) and rendering (sub-pixel compositing via
    draw_batch) are fully vectorised — handles 200+ simultaneous particles at
    60 fps without per-particle Python overhead.

    State fields per particle slot (capacity pre-allocated):
        pos      float32   fractional LED position
        vel      float32   LEDs/second
        accel    float32   LEDs/second² (gravity or other per-particle accel)
        age      float32   seconds since spawn
        lifespan float32   seconds until death
        color    float32[3]  RGB 0–255
        alive    bool
    """

    def __init__(self, capacity: int = 256, gravity: float = 0.0, drag: float = 0.96):
        """
        gravity   constant acceleration added to all alive particles (LEDs/s²)
        drag      velocity multiplier per virtual 30-fps frame (0.96 = light friction)
        """
        c = capacity
        self._pos      = np.zeros(c, dtype=np.float32)
        self._vel      = np.zeros(c, dtype=np.float32)
        self._accel    = np.zeros(c, dtype=np.float32)
        self._age      = np.zeros(c, dtype=np.float32)
        self._lifespan = np.ones(c,  dtype=np.float32)
        self._color    = np.zeros((c, 3), dtype=np.float32)
        self._alive    = np.zeros(c, dtype=bool)
        self._gravity  = float(gravity)
        self._drag     = float(drag)
        self._capacity = c
        self._n        = 1500   # updated in update()
        self._keep_alive = True  # set False to let the emitter expire when empty

    # ── Particle management ───────────────────────────────────────────────

    def _spawn(
        self,
        pos:      float,
        vel:      float,
        accel:    float,
        lifespan: float,
        color:    tuple,
    ) -> bool:
        """Spawn one particle in the first available slot.  Returns False if full."""
        dead = np.where(~self._alive)[0]
        if len(dead) == 0:
            return False
        i = dead[0]
        self._pos[i]      = float(pos)
        self._vel[i]      = float(vel)
        self._accel[i]    = float(accel)
        self._age[i]      = 0.0
        self._lifespan[i] = max(0.001, float(lifespan))
        self._color[i]    = [float(color[0]), float(color[1]), float(color[2])]
        self._alive[i]    = True
        return True

    def alive_count(self) -> int:
        return int(np.sum(self._alive))

    # ── LEDEntity interface ───────────────────────────────────────────────

    def update(self, dt: float, bpm: float, n: int) -> bool:
        self._n = n
        m = self._alive
        if not np.any(m):
            return self._keep_alive

        # Vectorised physics — all alive particles at once
        effective_accel = self._accel[m] + self._gravity
        self._vel[m]   += effective_accel * dt
        self._vel[m]   *= float(self._drag) ** (dt * 30.0)   # fps-normalised drag
        self._pos[m]   += self._vel[m] * dt
        self._age[m]   += dt

        # Wrap positions to strip
        self._pos[m] %= float(n)

        # Kill expired particles
        self._alive[m] = self._age[m] < self._lifespan[m]
        return self._keep_alive or bool(np.any(self._alive))

    def render(self, canvas: DDPCanvas) -> None:
        m = self._alive
        if not np.any(m):
            return
        # Per-particle brightness = fade curve over lifespan (quadratic ease-out)
        ratio       = self._age[m] / self._lifespan[m]
        brightness  = np.clip((1.0 - ratio) ** 2, 0.0, 1.0).astype(np.float32)
        canvas.draw_batch(self._pos[m], self._color[m], brightness, BlendMode.ADD)


# ─────────────────────────────────────────────────────────────────────────────
#  Explosion — ParticleEmitter triggered to burst N particles at a point
# ─────────────────────────────────────────────────────────────────────────────

class Explosion(ParticleEmitter):
    """Burst emitter: trigger() sprays N particles from source_index.

    Each particle gets a random velocity in [-max_v, +max_v].
    Gravity pulls all particles in one direction; drag makes them slow and bloom.
    """

    def __init__(
        self,
        source_index: float,
        color_primary:   tuple,
        color_secondary: tuple,
        n_particles:  int   = 80,
        max_v:        float = 600.0,   # LEDs/second
        lifespan:     float = 1.2,
        gravity:      float = 0.0,
        drag:         float = 0.94,
    ):
        super().__init__(capacity=max(n_particles, 32), gravity=gravity, drag=drag)
        self._keep_alive    = False    # expire when all particles die
        self._source        = float(source_index)
        self._c1            = color_primary
        self._c2            = color_secondary
        self._n_particles   = n_particles
        self._max_v         = float(max_v)
        self._lifespan_base = float(lifespan)
        self._triggered     = False

    def trigger(self, rng_seed: int = 0) -> None:
        if self._triggered:
            return
        self._triggered = True
        rng = np.random.default_rng(rng_seed)
        vels      = rng.uniform(-self._max_v, self._max_v, self._n_particles)
        lifespans = rng.uniform(self._lifespan_base * 0.5,
                                self._lifespan_base * 1.5, self._n_particles)
        for i in range(self._n_particles):
            col = self._c1 if rng.random() > 0.35 else self._c2
            self._spawn(self._source, float(vels[i]), 0.0,
                        float(lifespans[i]), col)


# ─────────────────────────────────────────────────────────────────────────────
#  Firework — rising head that spawns an Explosion on expiry
# ─────────────────────────────────────────────────────────────────────────────

class Firework(LEDEntity):
    """A single bright head that rises (toward index 0 or n) then explodes.

    The 'rise' phase draws the head directly.
    On expiry the head is replaced by an Explosion ParticleEmitter.
    The Firework entity stays alive until the explosion fully fades.
    """

    def __init__(
        self,
        start:       float,
        vel:         float,          # LEDs/second (negative = toward index 0)
        color_head:  tuple,
        color_burst: tuple,
        color_burst2: tuple,
        rise_time:   float = 0.8,    # seconds until explosion
        n_particles: int   = 60,
        max_v:       float = 500.0,
    ):
        self._pos        = float(start)
        self._vel        = float(vel)
        self._col_head   = color_head
        self._col_burst  = color_burst
        self._col_burst2 = color_burst2
        self._rise_time  = max(0.05, float(rise_time))
        self._age        = 0.0
        self._exploded   = False
        self._explosion: Optional[Explosion] = None
        self._n_particles = n_particles
        self._max_v       = float(max_v)

    def update(self, dt: float, bpm: float, n: int) -> bool:
        if not self._exploded:
            self._pos += self._vel * dt
            self._age += dt
            if self._age >= self._rise_time:
                self._exploded = True
                self._explosion = Explosion(
                    source_index    = self._pos,
                    color_primary   = self._col_burst,
                    color_secondary = self._col_burst2,
                    n_particles     = self._n_particles,
                    max_v           = self._max_v,
                    lifespan        = 1.4,
                    gravity         = 0.0,
                    drag            = 0.93,
                )
                self._explosion.trigger(rng_seed=int(self._pos * 100) & 0xFFFF)
            return True
        else:
            alive = self._explosion.update(dt, bpm, n)
            return alive

    def render(self, canvas: DDPCanvas) -> None:
        if not self._exploded:
            canvas.draw(self._pos, self._col_head, BlendMode.ADD, 1.0)
        else:
            self._explosion.render(canvas)


# ─────────────────────────────────────────────────────────────────────────────
#  GlitterEmitter — stationary sparkle burst seeded once per trigger()
# ─────────────────────────────────────────────────────────────────────────────

class GlitterEmitter(ParticleEmitter):
    """Stationary particles with very short lifespans — looks like glitter or confetti.

    trigger() seeds a burst of particles near source_index with tiny random offsets.
    """

    def __init__(
        self,
        source_index:  float,
        color_primary: tuple,
        color_secondary: tuple,
        radius:        float = 30.0,    # spread in LEDs
        n_particles:   int   = 100,
        lifespan:      float = 0.25,
        drag:          float = 0.75,    # very high drag — quick stop
    ):
        super().__init__(capacity=max(n_particles, 32), gravity=0.0, drag=drag)
        self._keep_alive   = False
        self._source       = float(source_index)
        self._c1           = color_primary
        self._c2           = color_secondary
        self._radius       = float(radius)
        self._n_particles  = n_particles
        self._lifespan_base = float(lifespan)

    def trigger(self, rng_seed: int = 0) -> None:
        rng = np.random.default_rng(rng_seed)
        offsets   = rng.uniform(-self._radius, self._radius, self._n_particles)
        vels      = rng.uniform(-self._radius * 2, self._radius * 2, self._n_particles)
        lifespans = rng.uniform(self._lifespan_base * 0.4,
                                self._lifespan_base * 1.8, self._n_particles)
        for i in range(self._n_particles):
            col = self._c1 if rng.random() > 0.3 else self._c2
            self._spawn(self._source + float(offsets[i]),
                        float(vels[i]), 0.0, float(lifespans[i]), col)


# ─────────────────────────────────────────────────────────────────────────────
#  AdvancedPattern — abstract frame-by-frame entity seeder
# ─────────────────────────────────────────────────────────────────────────────

class AdvancedPattern(ABC):
    """Named pattern that populates the entity list each frame.

    decay_rate   Controls how fast the canvas buffer fades per virtual 30-fps frame.
    use_blur     If True, canvas.blur() is called after all entities render.
    """

    decay_rate: float = 0.82
    use_blur:   bool  = False

    @abstractmethod
    def seed(
        self,
        t:          float,
        dt:         float,
        beat_phase: float,
        bpm:        float,
        n:          int,
        c1:         tuple,
        c2:         tuple,
        speed:      float,
        brightness: float,
        entities:   list,
    ) -> None:
        """Spawn or mutate entities as needed for this frame."""


# ─────────────────────────────────────────────────────────────────────────────
#  Concrete patterns
# ─────────────────────────────────────────────────────────────────────────────

class _MeteorStorm(AdvancedPattern):
    """8 bouncing meteors with slightly varied speeds, two palette colours."""
    decay_rate = 0.84

    def __init__(self):
        self._seeded = False

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        if self._seeded and entities:
            return
        self._seeded = True
        rng      = np.random.default_rng(42)
        base_vel = n * 0.55 * speed
        for i in range(8):
            vel = base_vel * rng.uniform(0.6, 1.4) * (1 if i % 2 == 0 else -1)
            col = c1 if i % 2 == 0 else c2
            br  = float(rng.uniform(0.7, 1.0)) * brightness
            entities.append(Meteor(float(rng.uniform(0, n)), vel, col, br,
                                   BlendMode.ADD, bounce=True))


class _SparkleField(AdvancedPattern):
    """Constant rain of short-lived sparkles at random positions."""
    decay_rate = 0.55
    use_blur   = True

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        rate = max(1, int(n * 0.04 * speed * dt * 30))
        rng  = np.random.default_rng(int(t * 1000) & 0xFFFFFF)
        for _ in range(rate):
            pos  = float(rng.integers(0, n))
            col  = c1 if rng.random() > 0.4 else c2
            life = float(rng.uniform(0.05, 0.22)) / max(0.1, speed)
            br   = float(rng.uniform(0.5, 1.0)) * brightness
            entities.append(Sparkle(pos, col, life, br))


class _CometPulse(AdvancedPattern):
    """Beat-synced: one new Meteor launched from a random position each beat."""
    decay_rate = 0.88

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        rng = np.random.default_rng(beat_idx + 99)
        vel = n * 0.9 * speed * (1 if rng.random() > 0.5 else -1)
        col = c1 if beat_idx % 2 == 0 else c2
        entities.append(Meteor(float(rng.integers(0, n)), vel, col,
                               brightness, BlendMode.ADD, bounce=True))


class _DualMeteor(AdvancedPattern):
    """Two meteors traveling in opposite directions — they collide and pass through."""
    decay_rate = 0.86

    def __init__(self):
        self._seeded = False

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        if self._seeded and entities:
            return
        self._seeded = True
        vel = n * 0.7 * speed
        entities.append(Meteor(0.0,       vel,  c1, brightness, BlendMode.ADD, bounce=True))
        entities.append(Meteor(n - 1.0,  -vel,  c2, brightness, BlendMode.ADD, bounce=True))


class _DeepSpace(AdvancedPattern):
    """Sparse slow meteors with very long decay — drifting star-field aesthetic."""
    decay_rate = 0.965

    def __init__(self):
        self._seeded = False

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        if self._seeded and entities:
            return
        self._seeded = True
        rng = np.random.default_rng(1337)
        for i in range(5):
            vel = n * float(rng.uniform(0.05, 0.18)) * speed * (1 if i % 2 == 0 else -1)
            col = c1 if i % 3 != 2 else c2
            br  = float(rng.uniform(0.4, 0.75)) * brightness
            entities.append(Meteor(float(rng.integers(0, n)), vel, col, br,
                                   BlendMode.ADD, bounce=True))


class _PulseExpand(AdvancedPattern):
    """Beat-synced expanding shockwaves from the centre."""
    decay_rate = 0.78

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        col   = c1 if beat_idx % 2 == 0 else c2
        vel   = n * 0.6 * speed
        sig   = max(4.0, n * 0.015)
        life  = 0.9 / max(0.1, speed)
        entities.append(Shockwave(n / 2.0, col,     n / 2.0, vel,  sig, brightness))
        entities.append(Shockwave(n / 2.0, col, n / 2.0, vel * 0.6, sig, brightness * 0.5))


class _ChaosSwarm(AdvancedPattern):
    """16 fast dim meteors with moderate decay — dense chaotic energy."""
    decay_rate = 0.76

    def __init__(self):
        self._seeded = False

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        if self._seeded and entities:
            return
        self._seeded = True
        rng = np.random.default_rng(9999)
        for i in range(16):
            vel = n * float(rng.uniform(0.8, 2.0)) * speed * (1 if rng.random() > 0.5 else -1)
            col = c1 if i % 3 != 2 else c2
            br  = float(rng.uniform(0.3, 0.7)) * brightness
            entities.append(Meteor(float(rng.integers(0, n)), vel, col, br,
                                   BlendMode.ADD, bounce=True))


class _FireflySwarm(AdvancedPattern):
    """Slow meandering meteors — organic, natural feel."""
    decay_rate = 0.91

    def __init__(self):
        self._seeded = False

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        if self._seeded and entities:
            return
        self._seeded = True
        rng = np.random.default_rng(8765)
        for i in range(10):
            vel = n * float(rng.uniform(0.03, 0.12)) * speed * (1 if rng.random() > 0.5 else -1)
            col = c1 if rng.random() > 0.35 else c2
            br  = float(rng.uniform(0.6, 1.0)) * brightness
            entities.append(Meteor(float(rng.integers(0, n)), vel, col, br,
                                   BlendMode.SCREEN, bounce=True))


class _BeatLaunch(AdvancedPattern):
    """Every beat: two meteors from both ends converging toward centre."""
    decay_rate = 0.87

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        vel  = n * 0.8 * speed
        cola = c1 if beat_idx % 2 == 0 else c2
        colb = c2 if beat_idx % 2 == 0 else c1
        entities.append(Meteor(0.0,       vel,  cola, brightness, BlendMode.ADD, bounce=True))
        entities.append(Meteor(n - 1.0,  -vel,  colb, brightness, BlendMode.ADD, bounce=True))


class _ExplosionBurst(AdvancedPattern):
    """Beat-triggered explosion at a random position each bar."""
    decay_rate = 0.82
    use_blur   = True

    def __init__(self):
        self._last_bar = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        bar_idx = int(t * bpm / 60.0 / 4)
        if bar_idx == self._last_bar:
            return
        self._last_bar = bar_idx
        rng = np.random.default_rng(bar_idx + 17)
        pos = float(rng.integers(n // 8, 7 * n // 8))
        exp = Explosion(
            source_index    = pos,
            color_primary   = c1,
            color_secondary = c2,
            n_particles     = max(60, int(80 * speed)),
            max_v           = n * 0.45 * speed,
            lifespan        = 1.3 / max(0.1, speed),
            gravity         = 0.0,
            drag            = 0.94,
        )
        exp.trigger(rng_seed=bar_idx)
        entities.append(exp)


class _FireworkShow(AdvancedPattern):
    """Beat-synced fireworks — each fires from a random launch position."""
    decay_rate = 0.87
    use_blur   = True

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        # Only launch every 2 beats to avoid overcrowding
        if beat_idx % 2 != 0:
            return
        rng    = np.random.default_rng(beat_idx + 7)
        start  = float(rng.integers(n // 6, 5 * n // 6))
        # Rise toward a random target in the opposite half
        target = float(rng.integers(n // 10, n - n // 10))
        dist   = target - start
        rt     = 0.6 / max(0.1, speed)
        vel    = dist / rt
        col_h  = c1
        col_b  = c1 if beat_idx % 4 == 0 else c2
        entities.append(Firework(
            start        = start,
            vel          = vel,
            color_head   = col_h,
            color_burst  = col_b,
            color_burst2 = c2,
            rise_time    = rt,
            n_particles  = max(50, int(70 * speed)),
            max_v        = n * 0.4 * speed,
        ))


class _GlitterBurst(AdvancedPattern):
    """Beat-triggered glitter emitter at a random position."""
    decay_rate = 0.60
    use_blur   = True

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        rng = np.random.default_rng(beat_idx + 55)
        pos = float(rng.integers(n // 8, 7 * n // 8))
        g   = GlitterEmitter(
            source_index    = pos,
            color_primary   = c1,
            color_secondary = c2,
            radius          = n * 0.08,
            n_particles     = max(80, int(100 * speed)),
            lifespan        = 0.3 / max(0.1, speed),
            drag            = 0.78,
        )
        g.trigger(rng_seed=beat_idx)
        entities.append(g)


class _ShockwavePulse(AdvancedPattern):
    """Beat-synced shockwave rings from both ends meeting at centre."""
    decay_rate = 0.80

    def __init__(self):
        self._last_beat = -1

    def seed(self, t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, entities):
        beat_idx = int(t * bpm / 60.0)
        if beat_idx == self._last_beat:
            return
        self._last_beat = beat_idx
        col   = c1 if beat_idx % 2 == 0 else c2
        vel   = n * 0.7 * speed
        sig   = max(5.0, n * 0.02)
        # Two shockwaves from edges toward centre
        entities.append(Shockwave(0.0,     col, n / 2.0, vel,  sig, brightness))
        entities.append(Shockwave(n - 1.0, col, n / 2.0, vel,  sig, brightness))


# ─────────────────────────────────────────────────────────────────────────────
#  Pattern registry
# ─────────────────────────────────────────────────────────────────────────────

_PATTERN_REGISTRY: dict[str, type[AdvancedPattern]] = {
    "meteor_storm":    _MeteorStorm,
    "sparkle_field":   _SparkleField,
    "comet_pulse":     _CometPulse,
    "dual_meteor":     _DualMeteor,
    "deep_space":      _DeepSpace,
    "pulse_expand":    _PulseExpand,
    "chaos_swarm":     _ChaosSwarm,
    "firefly_swarm":   _FireflySwarm,
    "beat_launch":     _BeatLaunch,
    "explosion_burst": _ExplosionBurst,
    "firework_show":   _FireworkShow,
    "glitter_burst":   _GlitterBurst,
    "shockwave_pulse": _ShockwavePulse,
}


# ─────────────────────────────────────────────────────────────────────────────
#  AdvancedRenderer — outer frame dispatcher
# ─────────────────────────────────────────────────────────────────────────────

class AdvancedRenderer:
    """Drives the DDPCanvas lifecycle for a registered AdvancedPattern.

    Same call signature as VaultRenderer — drop-in replacement:
        output_uint8 = renderer.render(t, bpm, params, beat_phase)

    params keys:
        pattern          str   pattern key (see _PATTERN_REGISTRY)
        n                int   LED count (default 1500)
        speed            float speed multiplier
        brightness       float brightness multiplier
        color_primary    list  [r, g, b]
        color_secondary  list  [r, g, b]
        blur             bool  override pattern's use_blur flag
    """

    def __init__(self):
        self._canvas:      Optional[DDPCanvas]      = None
        self._n:           int                      = 0
        self._entities:    List[LEDEntity]          = []
        self._pattern:     Optional[AdvancedPattern] = None
        self._pattern_key: Optional[str]            = None
        self._t_last:      Optional[float]          = None

    # ── Public API ────────────────────────────────────────────────────────

    def render(
        self,
        t:          float,
        bpm:        float,
        params:     dict,
        beat_phase: float = 0.0,
    ) -> np.ndarray:
        """Return uint8 (n, 3) frame.  Maintains persistent canvas state between calls."""
        pattern_key = params.get("pattern",           "meteor_storm")
        speed       = float(params.get("speed",       1.0))
        brightness  = float(params.get("brightness",  0.85))
        n           = int(  params.get("n",           1500))
        c1          = tuple(params.get("color_primary",   [255, 140, 20]))
        c2          = tuple(params.get("color_secondary", [20,  80, 220]))
        use_blur    = bool( params.get("blur",         False))
        tone_map    = str(  params.get("tone_map",    "white_hot"))
        tone_white  = float(params.get("tone_white",  512.0))

        # ── Compute dt ────────────────────────────────────────────────────
        if self._t_last is None:
            dt = 1 / 30
        else:
            dt = max(1e-4, min(t - self._t_last, 0.25))   # clamp to 4–250 ms
        self._t_last = t

        # ── Load / switch pattern ─────────────────────────────────────────
        if pattern_key != self._pattern_key:
            PatCls = _PATTERN_REGISTRY.get(pattern_key, _MeteorStorm)
            self._pattern     = PatCls()
            self._pattern_key = pattern_key
            self._entities.clear()
            if self._canvas is not None:
                self._canvas.reset()

        pat = self._pattern  # guaranteed not None after above

        # ── Ensure canvas matches strip length ────────────────────────────
        if self._canvas is None or self._n != n:
            self._canvas = DDPCanvas(EngineConfig(
                n          = n,
                decay_rate = pat.decay_rate,
                tone_map   = tone_map,
                tone_white = tone_white,
            ))
            self._n = n
            self._entities.clear()
        else:
            self._canvas.config.decay_rate = pat.decay_rate
            self._canvas.config.tone_map   = tone_map
            self._canvas.config.tone_white = tone_white

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        #  FRAME LIFECYCLE
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # 1. Decay — exponential fade creates motion-blur trails
        self._canvas.decay(dt)

        # 2. Seed — pattern spawns / reuses entities
        pat.seed(t, dt, beat_phase, bpm, n, c1, c2, speed, brightness, self._entities)

        # 3. Update — advance physics, cull expired entities
        self._entities = [e for e in self._entities if e.update(dt, bpm, n)]

        # 4. Render — each entity draws onto the canvas
        for e in self._entities:
            e.render(self._canvas)

        # 5. Blur (optional) — softens sharp particles into glowing embers
        if use_blur or pat.use_blur:
            self._canvas.blur()

        # 6. Clamp — safe uint8 for DDP
        return self._canvas.clamp()

    def reset(self) -> None:
        """Hard-reset all state — call when switching away from advanced mode."""
        if self._canvas is not None:
            self._canvas.reset()
        self._entities.clear()
        self._pattern     = None
        self._pattern_key = None
        self._t_last      = None
