"""Install the agent skills (/smart-commit, /smart-merge) into the personal skills folder of
every supported AI client (Cursor, Claude Code); both read the same SKILL.md format."""
from __future__ import annotations

import os
import shutil
import sys

from .hooks import ENTRY_SCRIPT

PROJECT_DIR = os.path.dirname(ENTRY_SCRIPT)
TEMPLATES_DIR = os.path.join(PROJECT_DIR, "skills")
SKILL_NAMES = ("smart-commit", "smart-merge")
CLIENTS = (("Cursor", ".cursor"), ("Claude Code", ".claude"))
CUSTOM = "Custom folder"


def default_roots() -> list[tuple[str, str]]:
    """(client, skills folder) for every supported client, installed or not."""
    home = os.path.expanduser("~")
    return [(client, os.path.join(home, folder, "skills")) for client, folder in CLIENTS]


def client_installed(root: str) -> bool:
    return os.path.isdir(os.path.dirname(root))


def roots(root: str | None = None) -> list[tuple[str, str]]:
    """The given folder, else the skills folder of every client found on this computer
    (all of them when none is found, so a later install of the client picks the skills up)."""
    if root:
        return [(CUSTOM, root)]
    found = [(c, r) for c, r in default_roots() if client_installed(r)]
    return found or default_roots()


def _dest(name: str, root: str) -> str:
    return os.path.join(root, name, "SKILL.md")


def render(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace("{{GITCHECK}}", ENTRY_SCRIPT.replace("\\", "/"))
    return text.replace("{{PYTHON}}", sys.executable.replace("\\", "/"))


def install(root: str | None = None) -> list[tuple[str, str]]:
    """Returns (client, installed SKILL.md path) pairs."""
    installed = []
    for client, r in roots(root):
        for name in SKILL_NAMES:
            dest = _dest(name, r)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render(name))
            installed.append((client, dest))
    return installed


def uninstall(root: str | None = None) -> list[str]:
    """Removes the skills from the given folder, else from every client's folder. Returns removed folders."""
    removed = []
    for _, r in ([(CUSTOM, root)] if root else default_roots()):
        for name in SKILL_NAMES:
            dest = _dest(name, r)
            if os.path.isfile(dest):
                shutil.rmtree(os.path.dirname(dest))
                removed.append(os.path.dirname(dest))
    return removed


def state(name: str, root: str) -> str:
    """'missing', 'outdated' (template changed or folder moved) or 'ok'."""
    try:
        with open(_dest(name, root), encoding="utf-8") as fh:
            installed = fh.read()
    except OSError:
        return "missing"
    return "ok" if installed == render(name) else "outdated"
