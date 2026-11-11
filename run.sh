#!/bin/bash
# Start the djlights app (CDJ monitor + LED control + AI)
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
if [ ! -x venv/bin/python3 ]; then
  echo "Run ./install.sh first."
  exit 1
fi
exec venv/bin/python3 -u wled_ctrl.py "$@"
