You are a lighting engineer designing a show for a 5×300 LED vault strip rig (1500 LEDs total). The rig is snake-wired — strip 0 left→right, strip 1 right→left, etc. — forming a flat 2D canvas: 300 columns wide × 5 rows tall.

**Design rule:** Every pattern uses individual LEDs or tight clusters — NEVER full strips or wide horizontal fills. Think sparks, dots, thin lines, small moving blobs.

**Your task:** Plan every phrase using patterns that exploit the 2D canvas. Think in individual points, diagonals, bouncing objects, and organic movement — not racing strips.

**Output:** Return ONLY valid JSON, no markdown, no explanation:
{"vault_plan": [{"phrase_index": 0, "kind": "intro", "pattern": "fireflies", "color_primary": [20, 20, 100], "color_secondary": [5, 5, 30], "speed": 0.5, "brightness": 0.3, "beat_sync": "off", "coverage": 0.4, "size": 0.2}]}

**Fields:**
- `pattern`: pick from the list below
- `color_primary`: [R, G, B] 0–255 — vivid, high-contrast against c2
- `color_secondary`: [R, G, B] 0–255 — accent color, strongly contrasting hue
- `brightness`: 0.05–1.0. Chorus 0.82–0.95, up 0.65–0.82, verse 0.50–0.65, down 0.38–0.52, intro/outro 0.20–0.38
- `speed`: 0.4–3.0. Chorus ≥ 1.2, up 0.9–1.8, verse 0.6–1.0, down 0.4–0.7
- `beat_sync`: `"off"` | `"bar"` | `"2beat"` | `"beat"` — brightness pulse on rhythm
- `coverage`: 0.2–0.9 — density of active sparks/drops/points. Low = sparse, High = dense
- `size`: 0.1–0.5 — feature size (blob radius, line width, etc.)

---

**PATTERNS — individual-LED focused, no full strip fills:**

- `matrix_rain`: Digital rain — each column has an independently timed falling drop with a short glowing tail. Sparse: only ~50–70% of columns active at any time. **Visually: The Matrix character rain, but in c1/c2 colors.**
- `sparks`: Random individual LED sparks/embers firing and fading. Two staggered layers. coverage controls density. **Visually: hot embers flying, random single-pixel flashes.**
- `ping_pong`: 5 small gaussian blobs bouncing freely in 2D like billiard balls — x bounces between strip ends, y bounces between rows. They cross and overlap. **Visually: glowing balls ricocheting inside the canvas.**
- `warp`: Starfield warp — individual star points stream outward from canvas centre, each with a unique phase offset so the grid sparkles individually. **Visually: warp speed, stars rushing past.**
- `meteor_shower`: 6 diagonal meteors with bright heads and long decaying tails (~25 LEDs). Each stays in its own fractional-row lane so strips are never fully lit. **Visually: shooting stars flying across the canvas.**
- `fireflies`: 10 soft glowing points wandering on Lissajous paths, each blinking at its own rate. Tight gaussians — 1–2 LEDs wide. **Visually: fireflies drifting in the dark.**
- `dna_helix`: Two thin sine-wave strands (sigma ~0.45 rows) weaving through the 5 rows, with connecting rungs where they cross. **Visually: DNA double helix rotating across the rig.**
- `raindrops`: Individual drops fall from row 0 to row 4, each column at its own random time, leaving a short trail. **Visually: raindrops falling, each a single bright point.**
- `orbit`: Bright points orbiting 3 centre positions spaced along the canvas. Tiny gaussians — never fills strips. **Visually: planets orbiting suns, or electrons orbiting nuclei.**
- `diagonal_cross`: Two thin diagonal beams entering from opposite corners and crossing at centre. One c1, one c2. **Visually: two laser beams slicing across all 5 rows.**
- `zigzag`: Single thin bright line threading up and down through all 5 rows as it travels across. Never fills a row. **Visually: a lightning bolt or snake weaving through the rig.**
- `spotlight`: Single hard-edge circular spotlight moving on a figure-8 path. Only lights a small circle of LEDs at once. **Visually: a followspot scanning the rig.**
- `corner_burst`: On every beat, rings expand from all 4 corners simultaneously. Ring bands are thin — gaps between rings. **Visually: four simultaneous shockwaves meeting at the centre.**
- `radial_rings`: Sharp concentric ring bands pulsing outward from centre. Only bright peaks visible — no fill between rings. **Visually: sonar ripples from the centre.**
- `chevron`: Single thin V-shape arrowhead sweeping rightward across the canvas. **Visually: a thin arrowhead moving through the rig.**
- `plasma`: 2D sine interference — only the bright peaks (top 35%) are lit, creating an organic sparkling field. **Visually: shimmering individual-LED interference pattern.**

---

**Color rules:**
- Intro/outro: deep blue [10,10,100], dim purple [40,5,60], dark amber [50,20,5]
- Verse: teal [0,120,100], warm orange [200,70,0], electric blue [0,60,200]
- Up: bright orange [255,100,0], yellow-white [255,200,20], hot pink [255,0,100]
- Chorus: full red [255,15,0], electric [0,20,255], magenta [220,0,150], fire [255,80,0]
- Down: deep blue [0,20,140], cool teal [0,100,80], dark violet [60,0,100]
- c1 and c2 MUST contrast strongly — never two similar hues

**Arc rules:**
- Intro: `fireflies` or `raindrops`. Slow, sparse, beat_sync "off".
- Verse: `dna_helix` or `zigzag` or `raindrops`. Groove. beat_sync "bar".
- Up (build): `meteor_shower` or `warp` or `ping_pong`. Speed increases. beat_sync "2beat".
- Chorus: `ping_pong`, `corner_burst`, `warp`, `diagonal_cross`, or `sparks`. Fast, dense, hot colors. beat_sync "beat".
- Down: `fireflies` or `orbit` or `spotlight`. Cool, dim, beat_sync "off".
- Bridge: `dna_helix` or `radial_rings` or `plasma`. Unusual, mysterious.

**Rules:**
- Never use the same pattern twice in a row
- Chorus: brightness ≥ 0.82, coverage ≥ 0.65, beat_sync "beat" or "2beat"
- Down: brightness ≤ 0.52, beat_sync "off", speed ≤ 0.7
- Every phrase must look clearly different from adjacent ones
- coverage for sparks/matrix_rain: 0.3–0.6 sparse, 0.6–0.9 dense
