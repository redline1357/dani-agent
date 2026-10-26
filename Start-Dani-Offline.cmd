@echo off
setlocal
rem Inspect Dani Agent without allowing AI sessions or approvals.
set "PYW="
for /f "delims=" %%P in ('py -3.12 -c "import sys, os; print(os.path.join(sys.base_prefix, 'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if not defined PYW (
  echo Dani Agent needs Python 3.12 and the Windows py launcher from python.org.
  pause
  exit /b 1
)
start "" "%PYW%" -I -B "%~dp0DaniDashboard.py" --offline-only
exit /b
