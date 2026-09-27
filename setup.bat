@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 gitcheck.py setup %*
    goto done
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if %errorlevel%==0 (
    python gitcheck.py setup %*
    goto done
)
echo Python 3.9 or newer is required. Download it from https://www.python.org/downloads/
echo During install, tick "Add python.exe to PATH", then run this file again.

:done
echo.
pause
