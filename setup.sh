#!/bin/sh
# Gitcheck setup for macOS / Linux / Git Bash:  sh setup.sh
# Saves your project folders, installs the /smart-commit and /smart-merge skills for
# Cursor and Claude Code (whichever is installed) and the guard hooks.
# Extra arguments go to "gitcheck.py setup", e.g.  sh setup.sh PROJECTS_FOLDER --yes
case "$0" in */*) cd "${0%/*}" || exit 1 ;; esac

if ! command -v git >/dev/null 2>&1; then
  echo "git is required: https://git-scm.com/downloads" >&2
  exit 1
fi

for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$c" gitcheck.py setup "$@"
  fi
done
echo "Python 3.9 or newer is required: https://www.python.org/downloads/" >&2
exit 1
