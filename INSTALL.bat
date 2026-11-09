@echo off
REM ============================================================================
REM  catalyst-bot one-click installer for Windows. Double-click this file.
REM  It installs Python and Git if missing (via winget), downloads the bot into
REM  C:\catalyst-bot, and runs the setup, which asks you for the Anthropic key.
REM ============================================================================
setlocal EnableDelayedExpansion
set "TARGET=C:\catalyst-bot"
set "REPO=https://github.com/jakebarnard9-dev/Trading-Bot.git"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Python311;%ProgramFiles%\Git\cmd"

where python >nul 2>&1
if errorlevel 1 (
  echo Installing Python 3.12 ...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
  set "PATH=!PATH!;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
)
where git >nul 2>&1
if errorlevel 1 (
  echo Installing Git ...
  winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements --silent
  set "PATH=!PATH!;%ProgramFiles%\Git\cmd"
)
where python >nul 2>&1 || (echo Python still not found. Close this window, open it again, and re-run INSTALL.bat. & pause & exit /b 1)
where git >nul 2>&1 || (echo Git still not found. Close this window, open it again, and re-run INSTALL.bat. & pause & exit /b 1)

if exist "%TARGET%\config.yaml" (
  echo Updating existing install in %TARGET% ...
  cd /d "%TARGET%" && git pull --ff-only
) else (
  echo Downloading the bot into %TARGET% ...
  git clone "%REPO%" "%TARGET%" || (echo Download failed. Check your internet connection. & pause & exit /b 1)
  cd /d "%TARGET%"
)

echo.
echo ===== Running setup (it will ask for your Anthropic API key) =====
powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%\deploy\windows-setup.ps1"
echo.
echo Done. To start the bot later: double-click %TARGET%\deploy\windows-start.bat
pause
