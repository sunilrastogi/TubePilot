@echo off
rem ============================================================
rem  Channel Lab - one-click setup (double-click this file)
rem  - checks Python, installs dependencies
rem  - creates the whole video-editing folder structure
rem  - creates your .env from .env.example
rem  Run with /auto to skip all prompts.
rem ============================================================
setlocal
cd /d "%~dp0"
title Channel Lab setup
set "AUTO=0"
if /i "%~1"=="/auto" set "AUTO=1"

echo.
echo === Channel Lab setup ===
echo.

rem ---------- 1. Python ----------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython
for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])"') do echo [ok] Python %%V
goto :deps

:nopython
echo [!!] Python 3.9 or newer was not found.
echo      Install it from https://www.python.org/downloads/
echo      and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
if "%AUTO%"=="0" start "" https://www.python.org/downloads/
if "%AUTO%"=="0" pause
exit /b 1

rem ---------- 2. Python packages ----------
:deps
echo.
echo Installing Python packages (Pillow, imageio-ffmpeg)...
%PY% -m pip install --user --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [!!] Package install failed. Check your internet connection and run setup.bat again.
  if "%AUTO%"=="0" pause
  exit /b 1
)
echo [ok] Packages installed

rem ---------- 3. .env ----------
echo.
if exist ".env" (
  echo [ok] .env already exists - left unchanged
) else (
  copy /y ".env.example" ".env" >nul
  echo [ok] Created .env from .env.example
)

rem ---------- 4. Folders ----------
echo.
echo Creating folders...
rem server.py --init builds data\channels.json (from CHANNEL in .env, if set) and, per channel,
rem data\channels\<id>\{style_reference, master_assets, edits, thumbs, projects}
rem plus a sample_video template with raw_video, claude_assets, user_assets,
rem "thumbnail and metadata" and final_edit. It also moves data from an older single-channel layout.
%PY% server.py --init
if errorlevel 1 (
  echo [!!] Could not create the folders. See the message above.
  if "%AUTO%"=="0" pause
  exit /b 1
)
echo [ok] Folders ready. Add more channels later in the app: Settings - Channels.

rem ---------- 5. Optional tools ----------
echo.
where claude >nul 2>nul
if errorlevel 1 (
  echo [--] Claude Code CLI not found. Optional: install it from https://claude.com/claude-code
  echo      then run  claude  and type /login   to use your Claude subscription instead of an API key.
) else (
  echo [ok] Claude Code CLI found - run  claude  and type /login once if you have not already
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [--] ffmpeg is not on PATH. Style analysis still works through the bundled copy.
  echo      The edit.py cutting helper needs ffmpeg and ffprobe on PATH:
  where winget >nul 2>nul
  if errorlevel 1 (
    echo      Download from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH.
  ) else (
    echo      Install with:  winget install Gyan.FFmpeg   then reopen the terminal.
  )
) else (
  echo [ok] ffmpeg found
)

echo.
echo ============================================================
echo  Setup complete.
echo  Next:
echo   1. Add your credentials - see README.md section "Credentials"
echo      ^(you can paste them in the app's Settings tab or edit .env^)
echo   2. Double-click run.bat to start the app
echo ============================================================
echo.
if "%AUTO%"=="1" exit /b 0
choice /c YN /m "Start the app now"
if errorlevel 2 goto :end
call run.bat
:end
pause
