"""Install the /smart-commit Cursor skill into the user's personal skills folder."""
from __future__ import annotations

import os
import shutil
import sys

from .hooks import ENTRY_SCRIPT

PROJECT_DIR = os.path.dirname(ENTRY_SCRIPT)
TEMPLATE = os.path.join(PROJECT_DIR, "skill", "SKILL.md")
SKILL_NAME = "smart-commit"


def default_target() -> str:
    return os.path.join(os.path.expanduser("~"), ".cursor", "skills", SKILL_NAME)


def render() -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace("{{GITCHECK}}", ENTRY_SCRIPT.replace("\\", "/"))
    return text.replace("{{PYTHON}}", sys.executable.replace("\\", "/"))


def install(target_dir: str | None = None) -> str:
    target_dir = target_dir or default_target()
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, "SKILL.md")
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render())
    return dest


def uninstall(target_dir: str | None = None) -> bool:
    target_dir = target_dir or default_target()
    if not os.path.isfile(os.path.join(target_dir, "SKILL.md")):
        return False
    shutil.rmtree(target_dir)
    return True


def state(target_dir: str | None = None) -> str:
    """'missing', 'outdated' (template changed or folder moved) or 'ok'."""
    dest = os.path.join(target_dir or default_target(), "SKILL.md")
    try:
        with open(dest, encoding="utf-8") as fh:
            installed = fh.read()
    except OSError:
        return "missing"
    return "ok" if installed == render() else "outdated"
