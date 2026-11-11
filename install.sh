#!/bin/bash
# djlights installer (macOS)
#
#   ./install.sh            → installs, BLT picks the network interface itself
#   ./install.sh en5        → installs and pins BLT to interface en5 (where your CDJs are)
#
# Find your CDJ interface with:  networksetup -listallhardwareports
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
CDJ_IFACE="${1:-}"

echo "=== djlights install ==="

# ── Python 3.10+ with tkinter ────────────────────────────────────────────────
PYTHON=""
NO_TK=""
for cand in python3.12 python3.11 python3.10 python3.13 python3; do
  p=$(command -v "$cand" 2>/dev/null || true)
  [ -z "$p" ] && continue
  "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || continue
  if "$p" -c 'import tkinter' 2>/dev/null; then
    PYTHON="$p"; break
  else
    NO_TK="$NO_TK $cand"
  fi
done
if [ -z "$PYTHON" ]; then
  echo "ERROR: no Python 3.10+ with tkinter found (tkinter is needed for the UI)."
  [ -n "$NO_TK" ] && echo "       Found without tkinter:$NO_TK"
  echo "       Fix with Homebrew:  brew install python@3.12 python-tk@3.12"
  exit 1
fi
echo "Using $PYTHON ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# ── venv + dependencies ──────────────────────────────────────────────────────
if [ ! -d venv ]; then
  echo "Creating venv..."
  "$PYTHON" -m venv venv
fi
echo "Installing dependencies (takes a few minutes)..."
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt

# ── Beat Link Trigger show file with this folder's absolute path ─────────────
sed -e "s|__DJLIGHTS_DIR__|$DIR|g" -e "s|__CDJ_IFACE__|$CDJ_IFACE|g" \
    blt/djlights.blt.template > blt/djlights.blt
if [ -z "$CDJ_IFACE" ]; then
  # no interface given → drop the key so BLT chooses automatically
  sed -i '' -e 's|:network-interface "", ||' blt/djlights.blt
fi
sed -i '' -e "s|(def vp-cdj-iface \"[^\"]*\")|(def vp-cdj-iface \"$CDJ_IFACE\")|" blt/vp_blt_code.clj

chmod +x run.sh

echo ""
echo "=== Install complete ==="
echo ""
echo "Next:"
echo "  1. Open Beat Link Trigger 8.x → File → Open → $DIR/blt/djlights.blt"
echo "  2. Start the app:  ./run.sh"
echo "  3. In the app: AI tab → paste your Anthropic API key → Initialize AI"
echo "  See README.md for details."
