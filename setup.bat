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

rem ---------- 3. Folder structure ----------
echo.
echo Creating folders...
for %%D in (
  "data\style_reference"
  "data\master_assets"
  "data\thumbs"
  "data\projects"
  "data\edits\sample_video\raw_video"
  "data\edits\sample_video\claude_assets"
  "data\edits\sample_video\user_assets"
  "data\edits\sample_video\thumbnail and metadata"
  "data\edits\sample_video\final_edit"
) do if not exist %%D mkdir %%D
echo [ok] data\style_reference     - put 1-5 videos whose editing style you like here
echo [ok] data\master_assets       - your photos, logos, reusable assets
echo [ok] data\edits\sample_video  - template: one folder per video you edit, with
echo                                 raw_video, claude_assets, user_assets,
echo                                 "thumbnail and metadata", final_edit

rem ---------- 4. .env ----------
echo.
if exist ".env" (
  echo [ok] .env already exists - left unchanged
) else (
  copy /y ".env.example" ".env" >nul
  echo [ok] Created .env from .env.example
)

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
