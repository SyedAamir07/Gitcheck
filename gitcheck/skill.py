"""Install the Cursor skills (/smart-commit, /smart-merge) into the user's personal skills folder."""
from __future__ import annotations

import os
import shutil
import sys

from .hooks import ENTRY_SCRIPT

PROJECT_DIR = os.path.dirname(ENTRY_SCRIPT)
TEMPLATES_DIR = os.path.join(PROJECT_DIR, "skills")
SKILL_NAMES = ("smart-commit", "smart-merge")


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".cursor", "skills")


def _dest(name: str, root: str | None) -> str:
    return os.path.join(root or default_root(), name, "SKILL.md")


def render(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace("{{GITCHECK}}", ENTRY_SCRIPT.replace("\\", "/"))
    return text.replace("{{PYTHON}}", sys.executable.replace("\\", "/"))


def install(root: str | None = None) -> list[str]:
    installed = []
    for name in SKILL_NAMES:
        dest = _dest(name, root)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(name))
        installed.append(dest)
    return installed


def uninstall(root: str | None = None) -> list[str]:
    removed = []
    for name in SKILL_NAMES:
        dest = _dest(name, root)
        if os.path.isfile(dest):
            shutil.rmtree(os.path.dirname(dest))
            removed.append(name)
    return removed


def state(name: str, root: str | None = None) -> str:
    """'missing', 'outdated' (template changed or folder moved) or 'ok'."""
    try:
        with open(_dest(name, root), encoding="utf-8") as fh:
            installed = fh.read()
    except OSError:
        return "missing"
    return "ok" if installed == render(name) else "outdated"
