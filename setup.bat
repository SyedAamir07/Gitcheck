@echo off
rem Gitcheck setup for Windows: double-click this file, or run it from a terminal.
rem Saves your project folders, installs the /smart-commit and /smart-merge skills for
rem Cursor and Claude Code (whichever is installed) and the guard hooks.
rem Extra arguments go to "gitcheck.py setup", e.g.  setup.bat PROJECTS_FOLDER --yes
setlocal
cd /d "%~dp0"
set "RC=1"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

set "PY="
py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>nul && set "PY=python"
if not defined PY goto no_python

where git >nul 2>nul || goto no_git

%PY% gitcheck.py setup %*
set "RC=%errorlevel%"
goto done

:no_python
echo Python 3.9 or newer is required. Download it from https://www.python.org/downloads/
echo During install, tick "Add python.exe to PATH", then run this file again.
goto done

:no_git
echo git is required. Download it from https://git-scm.com/downloads then run this file again.

:done
echo.
pause
exit /b %RC%
