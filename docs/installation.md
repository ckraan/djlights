# Installation

← [Back to README](../README.md)

## 1. Prerequisites

### Python 3.10+ with tkinter

The UI is built on customtkinter, so Python needs tkinter. Homebrew's Python
does **not** include it by default:

```bash
brew install python@3.12 python-tk@3.12
```

`install.sh` picks the first Python (3.12, 3.11, 3.10, 3.13 or `python3`) that
is 3.10 or newer *and* has tkinter. If none qualifies, it stops with a clear
message.

### Beat Link Trigger 8.x

Download it from
[Deep-Symmetry/beat-link-trigger releases](https://github.com/Deep-Symmetry/beat-link-trigger/releases).
The included show file was built with BLT **8.0.0**.

### Network

- The Mac and the CDJs must be on the same Pro DJ Link network, normally
  through a switch.
- WLED controllers can be on any network the Mac can reach. Each one needs a
  fixed IP (a DHCP reservation, or set it in WLED).
- If the Mac has several interfaces (Wi-Fi plus a USB-ethernet adapter to the
  CDJs), find the one the CDJs are on:

  ```bash
  networksetup -listallhardwareports
  ```

### rekordbox

djlights depends on **phrase analysis**. In rekordbox, analyse tracks with
phrase analysis enabled, then export to USB or SD as usual.

## 2. Install

```bash
git clone https://github.com/ckraan/djlights.git
cd djlights
./install.sh            # BLT picks the network interface itself
# or
./install.sh en5        # pin BLT to the CDJ interface
```

The script:

1. creates `venv/` and installs the pinned dependencies from `requirements.txt`
2. generates `blt/djlights.blt` from `blt/djlights.blt.template`, filling in the
   absolute path of this folder
3. writes the chosen interface into `blt/vp_blt_code.clj` (`vp-cdj-iface`)

Run `./install.sh` again whenever you move the folder or change the interface.

## 3. First run

1. **Open the BLT show:** start Beat Link Trigger → *File → Open* →
   `blt/djlights.blt`. Within a few seconds, the BLT console shows lines like:
   ```
   [VP] BLT overlay server started (start-server 17081)
   [VP] DJ-Link OK: 2 devices found — no bind needed
   ```
2. **Start djlights:**
   ```bash
   ./run.sh
   ```
   The window opens on **CDJ MONITOR**. Load a track on a CDJ and its phrases
   and beat counter appear.
3. **Add your LEDs:** in **LED CONTROL**, click **+ ADD DEVICE** and set:
   - name and IP of the WLED controller
   - LED count
   - geometry: `linear`, `snake` (N strips wired back and forth) or `globe`

   Make sure the WLED controller accepts realtime data (*WLED → Config → Sync
   Interfaces → Realtime*). DDP on port 4048 is the WLED default.
4. **AI** *(optional)*: in **AI CONTROL**, paste your Anthropic API key and click
   **Initialize AI**. When a track loads, a show plan is generated for it.

### No CDJs at hand?

Click **▶ DEMO** in the title bar. It simulates a playing set from
`demo_plan.json`, so you can test devices, geometry and effects.

## 4. Updating

```bash
git pull
venv/bin/pip install -r requirements.txt
./install.sh            # regenerates blt/djlights.blt
```

Then reopen `blt/djlights.blt` in Beat Link Trigger.
