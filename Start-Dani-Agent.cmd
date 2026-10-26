@echo off
setlocal
rem Dani Agent needs Python 3.12 (python.org, installed for all users). The py launcher finds it.
set "PYW="
for /f "delims=" %%P in ('py -3.12 -c "import sys, os; print(os.path.join(sys.base_prefix, 'pythonw.exe'))" 2^>nul') do set "PYW=%%P"
if not defined PYW (
  echo Dani Agent needs Python 3.12 from python.org ^(install it for all users^).
  pause
  exit /b 1
)
start "" "%PYW%" -I -B "%~dp0DaniAgent.py"
exit /b
