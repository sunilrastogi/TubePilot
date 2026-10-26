#!/usr/bin/env bash
# ============================================================
#  TubePilot - one-click setup for macOS (and Linux)
#  macOS: double-click this file in Finder, or run:  ./setup.command
#  - checks Python 3.9+, creates a virtual environment (.venv), installs dependencies
#  - creates your .env from .env.example
#  - creates the whole video-editing folder structure
#  Run with --auto to skip all prompts.
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1
AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1

pause() {
  if [ "$AUTO" = 0 ]; then read -r -p "Press Enter to close... " _; fi
}

echo
echo "=== TubePilot setup ==="
echo

# ---------- 1. Python ----------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[!!] Python 3.9 or newer was not found."
  echo "     macOS: install it from https://www.python.org/downloads/macos/  or run:  brew install python"
  echo "     (if macOS offers to install the command line developer tools, accept, then run this again)"
  if [ "$AUTO" = 0 ] && command -v open >/dev/null 2>&1; then open "https://www.python.org/downloads/macos/"; fi
  pause
  exit 1
fi
echo "[ok] $("$PY" --version 2>&1)"

# ---------- 2. Virtual environment + packages ----------
# A venv avoids the "externally-managed-environment" error that Homebrew / newer system Pythons give for pip --user.
echo
if [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python ]; then
  echo "Creating the virtual environment (.venv)..."
  if ! "$PY" -m venv .venv; then
    echo "[!!] Could not create the virtual environment."
    echo "     Debian/Ubuntu: sudo apt install python3-venv     then run this again."
    pause
    exit 1
  fi
fi
VPY=.venv/bin/python
[ -x "$VPY" ] || VPY=.venv/Scripts/python
echo "Installing Python packages (Pillow, imageio-ffmpeg, certifi)..."
if ! "$VPY" -m pip install --disable-pip-version-check -r requirements.txt; then
  echo "[!!] Package install failed. Check your internet connection and run setup again."
  pause
  exit 1
fi
echo "[ok] Packages installed"

# ---------- 3. .env ----------
echo
if [ -f .env ]; then
  echo "[ok] .env already exists - left unchanged"
else
  cp .env.example .env
  echo "[ok] Created .env from .env.example"
fi

# ---------- 4. Folders ----------
# server.py --init builds data/channels.json (from CHANNEL in .env, if set) and, per channel,
# data/channels/<id>/{style_reference, master_assets, edits, thumbs, projects}
# plus a sample_video template with raw_video, claude_assets, user_assets,
# "thumbnail and metadata" and final_edit. It also moves data from an older single-channel layout.
echo
echo "Creating folders..."
if ! "$VPY" server.py --init; then
  echo "[!!] Could not create the folders. See the message above."
  pause
  exit 1
fi
echo "[ok] Folders ready. Add more channels later in the app: Settings - Channels."

# make sure the launcher is executable (a ZIP download can lose the permission)
chmod +x run.command setup.command 2>/dev/null || true

# ---------- 5. Optional tools ----------
echo
if command -v claude >/dev/null 2>&1; then
  echo "[ok] Claude Code CLI found - run  claude  and type /login once if you have not already"
else
  echo "[--] Claude Code CLI not found. Optional: install it from https://claude.com/claude-code"
  echo "     then run  claude  and type /login   to use your Claude subscription instead of an API key."
fi
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  echo "[ok] ffmpeg found"
else
  echo "[--] ffmpeg is not on PATH. Style analysis still works through the bundled copy."
  echo "     The edit.py cutting helper needs ffmpeg and ffprobe:"
  if command -v brew >/dev/null 2>&1; then
    echo "     install with:  brew install ffmpeg"
  else
    echo "     macOS: install Homebrew (https://brew.sh), then:  brew install ffmpeg"
  fi
fi

echo
echo "============================================================"
echo " Setup complete."
echo " Next:"
echo "  1. Add your credentials - see README.md section \"Credentials\""
echo "     (you can paste them in the app's Settings tab or edit .env)"
echo "  2. Double-click run.command (or run ./run.command) to start the app"
echo "============================================================"
echo
if [ "$AUTO" = 1 ]; then exit 0; fi
read -r -p "Start TubePilot now? [Y/n] " answer
case "${answer:-Y}" in
  [Nn]*) ;;
  *) exec ./run.command ;;
esac
pause
