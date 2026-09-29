"""Commit messages that sound local-only, and memory of which files they covered.

A flag sticks to the commit's files for a while, so rewording the message does not clear it:
the files must be re-checked and dropped, or the plan must say why they belong on the server.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time

LOCAL_MESSAGE_RE = re.compile(
    r"\b(?:local(?:host)?|locally|my\s+(?:machine|computer|pc|laptop|phone)|(?:on|from)\s+(?:my\s+|the\s+)?phones?"
    r"|firewall|port[\s-]?forward\w*|open(?:s|ing)?\s+ports?|lan|emulator|dev\s+machine|run(?:ning)?\s+locally"
    r"|debug[\s-]only|testing[\s-]only|temporary|temp|workaround\s+for\s+(?:my|this)\s+\w+)\b",
    re.I,
)
STORE_ENV = "GITCHECK_FLAGS_FILE"
TTL_SECONDS = 12 * 3600


def store_path() -> str:
    return os.environ.get(STORE_ENV) or os.path.join(tempfile.gettempdir(), "gitcheck-local-flags.json")


def _load() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        with open(store_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _key(repo: str) -> str:
    return os.path.normcase(os.path.abspath(repo))


def check(repo: str, message: str, files: list[str]) -> str | None:
    """Why this commit is flagged as local-only, or None. Records the files when the message
    sounds local-only; a later commit with any of those files stays flagged whatever its message."""
    data = _load()
    now = time.time()
    entries = {p: v for p, v in data.get(_key(repo), {}).items()
               if isinstance(v, dict) and now - v.get("ts", 0) < TTL_SECONDS}
    flag = None
    m = LOCAL_MESSAGE_RE.search(message)
    if m:
        flag = f"message sounds local-only (\"{m.group(0)}\")"
        for f in files:
            entries[f] = {"word": m.group(0), "ts": now}
    else:
        earlier = sorted(f for f in files if f in entries)
        if earlier:
            flag = (f"{earlier[0]} was in a commit whose message sounded local-only "
                    f"(\"{entries[earlier[0]]['word']}\"); a new message does not clear that")
    data[_key(repo)] = entries
    _save(data)
    return flag
