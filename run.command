#!/usr/bin/env bash
# TubePilot launcher for macOS (and Linux). macOS: double-click this file, or run:  ./run.command
cd "$(dirname "$0")" || exit 1

# first run (or a fresh clone): set everything up automatically
if [ ! -f .env ] || { [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python ]; }; then
  echo "First run - running setup..."
  bash ./setup.command --auto || exit 1
fi
VPY=.venv/bin/python
[ -x "$VPY" ] || VPY=.venv/Scripts/python

PORT="${PORT:-8791}"
URL="http://127.0.0.1:$PORT"
echo "Starting TubePilot on $URL  (press Ctrl+C or close this window to stop)"
(
  sleep 2
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) >/dev/null 2>&1 &
PORT="$PORT" "$VPY" server.py
echo
echo "The server stopped. If it failed to start, port $PORT may be in use - see README Troubleshooting."
read -r -p "Press Enter to close... " _
