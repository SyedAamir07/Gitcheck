"""Install/uninstall pre-commit and pre-push hooks that run the guard.

An existing non-gitcheck hook is kept as <name>.local and chained before the guard.
"""
from __future__ import annotations

import os
import stat
import sys

from .gitops import git, git_path

MARKER = "# gitcheck-managed-hook"
HOOK_NAMES = ("pre-commit", "pre-push")
ENTRY_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitcheck.py")

# Only shell builtins + exec on the common path: Git for Windows' sh cannot fork in
# some sandboxed/locked-down environments, and a hook that forks would then fail.
_TEMPLATE = """#!/bin/sh
{marker}
GITCHECK="{script}"
PY="{python}"
HOOK_DIR="${{0%/*}}"

if [ ! -f "$PY" ]; then
  PY=""
  for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ] || [ ! -f "$GITCHECK" ]; then
  echo "gitcheck: python or $GITCHECK not found - skipping {name} guard" >&2
  exit 0
fi
{body}
"""

_PRE_COMMIT_BODY = """if [ -x "$HOOK_DIR/pre-commit.local" ]; then
  "$HOOK_DIR/pre-commit.local" "$@" || exit $?
fi
exec "$PY" "$GITCHECK" guard --staged"""

_PRE_PUSH_BODY = """if [ -x "$HOOK_DIR/pre-push.local" ]; then
  INPUT=$(cat)
  printf '%s\\n' "$INPUT" | "$HOOK_DIR/pre-push.local" "$@" || exit $?
  printf '%s\\n' "$INPUT" | "$PY" "$GITCHECK" guard --pre-push
  exit $?
fi
exec "$PY" "$GITCHECK" guard --pre-push"""


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _render(name: str) -> str:
    body = _PRE_COMMIT_BODY if name == "pre-commit" else _PRE_PUSH_BODY
    return _TEMPLATE.format(
        marker=MARKER,
        script=_posix(ENTRY_SCRIPT),
        python=_posix(sys.executable),
        name=name,
        body=body,
    )


def _is_ours(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return MARKER in fh.read()
    except OSError:
        return False


def hooks_dir(repo: str) -> str:
    return git_path("hooks", repo)


def hooks_installed(repo: str) -> bool:
    d = hooks_dir(repo)
    return all(_is_ours(os.path.join(d, n)) for n in HOOK_NAMES)


def hooks_current(repo: str) -> bool:
    """Installed and pointing at this copy of gitcheck (false after the folder was moved)."""
    d = hooks_dir(repo)
    for n in HOOK_NAMES:
        try:
            with open(os.path.join(d, n), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        if MARKER not in text or f'GITCHECK="{_posix(ENTRY_SCRIPT)}"' not in text:
            return False
    return True


def custom_hooks_path(repo: str) -> str | None:
    """core.hooksPath if set (husky, lefthook, ...): those folders are usually tracked files."""
    value = git("config", "--get", "core.hooksPath", cwd=repo, check=False).strip()
    return value or None


def install(repo: str, force: bool = False) -> list[str]:
    git("rev-parse", "--git-dir", cwd=repo)
    custom = custom_hooks_path(repo)
    if custom and not force:
        return [
            f"skipped: core.hooksPath is set to '{custom}' (husky/lefthook/...). "
            f"Add this command to that tool's pre-commit hook instead: "
            f'"{_posix(sys.executable)}" "{_posix(ENTRY_SCRIPT)}" guard --staged   '
            f"(or rerun with --force to write into '{custom}')"
        ]
    d = hooks_dir(repo)
    os.makedirs(d, exist_ok=True)
    notes = []
    for name in HOOK_NAMES:
        path = os.path.join(d, name)
        local = path + ".local"
        if os.path.exists(path) and not _is_ours(path):
            if os.path.exists(local):
                notes.append(f"{name}: existing hook left untouched ({name}.local already exists)")
                continue
            os.replace(path, local)
            notes.append(f"{name}: existing hook kept as {name}.local and chained")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_render(name))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        notes.append(f"{name}: installed")
    return notes


def uninstall(repo: str) -> list[str]:
    d = hooks_dir(repo)
    notes = []
    for name in HOOK_NAMES:
        path = os.path.join(d, name)
        local = path + ".local"
        if _is_ours(path):
            os.remove(path)
            if os.path.exists(local):
                os.replace(local, path)
                notes.append(f"{name}: removed, original hook restored")
            else:
                notes.append(f"{name}: removed")
        else:
            notes.append(f"{name}: not a gitcheck hook, left untouched")
    return notes
