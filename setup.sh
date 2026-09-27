#!/bin/sh
# Gitcheck setup for macOS / Linux / Git Bash:  sh setup.sh
cd "$(dirname "$0")" || exit 1
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    exec "$c" gitcheck.py setup "$@"
  fi
done
echo "Python 3.9 or newer is required: https://www.python.org/downloads/" >&2
exit 1
