"""Per-user settings: the list of project repos chosen during setup.

Stored in ~/.gitcheck/config.json (override with the GITCHECK_CONFIG env var).
"""
from __future__ import annotations

import json
import os


def config_path() -> str:
    return os.environ.get("GITCHECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".gitcheck", "config.json")


def load() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("projects", [])
    return data


def save(data: dict) -> str:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return path


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def projects() -> list[str]:
    return list(load()["projects"])


def add_projects(paths: list[str]) -> list[str]:
    data = load()
    known = {_key(p) for p in data["projects"]}
    added = []
    for p in paths:
        p = os.path.normpath(os.path.abspath(p))
        if _key(p) not in known:
            data["projects"].append(p)
            known.add(_key(p))
            added.append(p)
    save(data)
    return added


def remove_projects(paths: list[str]) -> list[str]:
    data = load()
    drop = {_key(os.path.abspath(p)) for p in paths}
    removed = [p for p in data["projects"] if _key(p) in drop]
    data["projects"] = [p for p in data["projects"] if _key(p) not in drop]
    save(data)
    return removed
