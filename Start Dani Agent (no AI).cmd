@echo off
setlocal
rem Dani Agent without AI: look around for free. AI sessions and approvals stay switched off.
rem It needs Python 3.12 from python.org, installed for all users. The Windows "py" launcher finds it.
set "PYW="
for /f "delims=" %%P in ('py -3.12 -c "import sys, os; print(os.path.join(sys.base_prefix, 'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if not defined PYW (
  echo.
  echo  Dani Agent needs Python 3.12, and it was not found on this PC.
  echo.
  echo  1. Download Python 3.12 from https://www.python.org/downloads/windows/
  echo  2. In the installer choose "Customize installation" and tick
  echo     "py launcher" and "Install Python 3.12 for all users".
  echo  3. Double-click this file again.
  echo.
  echo  QUICK_START.md in this folder lists everything else Dani Agent needs.
  echo.
  pause
  exit /b 1
)
start "" "%PYW%" -I -B "%~dp0DaniDashboard.py" --offline-only
exit /b
