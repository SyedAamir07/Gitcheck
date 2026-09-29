"""Snapshot of every repo: branch, uncommitted changes with line stats, guard findings."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from . import guard
from .gitops import EMPTY_TREE, git, git_ok, has_commit, operation_in_progress
from .hooks import hooks_installed


@dataclass
class FileChange:
    path: str
    status: str  # porcelain XY code, "??" for untracked
    added: int | None = None
    deleted: int | None = None
    orig_path: str | None = None
    note: str | None = None


@dataclass
class RepoStatus:
    path: str
    branch: str
    upstream: str | None
    ahead: int | None
    behind: int | None
    unpushed_commits: int
    operation: str | None
    hooks_installed: bool
    changes: list[FileChange] = field(default_factory=list)
    findings: list[guard.Finding] = field(default_factory=list)
    error: str | None = None


def porcelain(repo: str) -> list[FileChange]:
    raw = git("status", "--porcelain=v1", "-z", "-uall", cwd=repo)
    entries = raw.split("\0")
    out: list[FileChange] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        fc = FileChange(path=path, status=code)
        if "R" in code or "C" in code:
            fc.orig_path = entries[i] if i < len(entries) else None
            i += 1
        out.append(fc)
    return out


def _numstat(repo: str) -> dict[str, tuple[int | None, int | None]]:
    base = "HEAD" if has_commit("HEAD", repo) else EMPTY_TREE
    stats: dict[str, tuple[int | None, int | None]] = {}
    for item in git("diff", base, "--numstat", "--no-renames", "-z", cwd=repo).split("\0"):
        parts = item.strip("\n").split("\t")
        if len(parts) == 3:
            a, d, p = parts
            stats[p] = (None if a == "-" else int(a), None if d == "-" else int(d))
    return stats


def _count_lines(full: str) -> int | None:
    try:
        if os.path.getsize(full) > guard.MAX_UNTRACKED_BYTES:
            return None
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


def _upstream(repo: str) -> tuple[str | None, int | None, int | None]:
    if not git_ok("rev-parse", "--abbrev-ref", "@{u}", cwd=repo):
        return None, None, None
    upstream = git("rev-parse", "--abbrev-ref", "@{u}", cwd=repo).strip()
    behind, ahead = git("rev-list", "--left-right", "--count", "@{u}...HEAD", cwd=repo).split()
    return upstream, int(ahead), int(behind)


def file_changes(repo: str) -> list[FileChange]:
    """Uncommitted changes with +/- line counts."""
    changes = porcelain(repo)
    stats = _numstat(repo)
    for fc in changes:
        if fc.status == "??":
            fc.added, fc.deleted = _count_lines(os.path.join(repo, fc.path)), 0
        elif fc.path in stats:
            fc.added, fc.deleted = stats[fc.path]
            if fc.added is None:
                fc.note = "binary"
        elif "D" not in fc.status:
            fc.note = "no content diff (line endings or file mode only)"
    return changes


def repo_status(repo: str) -> RepoStatus:
    repo = os.path.abspath(repo)
    try:
        branch = git("branch", "--show-current", cwd=repo).strip() or "(detached)"
        upstream, ahead, behind = _upstream(repo)
        unpushed = 0
        if has_commit("HEAD", repo):
            unpushed = int(git("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=repo).strip() or 0)
        st = RepoStatus(
            path=repo,
            branch=branch,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            unpushed_commits=unpushed,
            operation=operation_in_progress(repo),
            hooks_installed=hooks_installed(repo),
        )
        st.changes = file_changes(repo)
        st.findings = guard.run_checks(repo, guard.collect_worktree(repo))
        return st
    except Exception as exc:  # one broken repo must not hide the others
        return RepoStatus(repo, "?", None, None, None, 0, None, False, error=str(exc))


def to_dict(st: RepoStatus) -> dict:
    return asdict(st)


def format_status(st: RepoStatus) -> str:
    lines = [f"== {st.path}"]
    if st.error:
        lines.append(f"  ERROR: {st.error}")
        return "\n".join(lines)
    track = f" -> {st.upstream} (ahead {st.ahead}, behind {st.behind})" if st.upstream else " (no upstream)"
    lines.append(f"  branch: {st.branch}{track}; unpushed commits: {st.unpushed_commits}")
    if st.operation:
        lines.append(f"  WARNING: {st.operation} in progress - finish it before committing")
    lines.append(f"  gitcheck hooks: {'installed' if st.hooks_installed else 'NOT installed'}")
    if not st.changes:
        lines.append("  no uncommitted changes")
    else:
        lines.append(f"  {len(st.changes)} changed file(s):")
        for fc in st.changes:
            stat = ""
            if fc.added is not None or fc.deleted is not None:
                stat = f"  (+{fc.added if fc.added is not None else '?'}/-{fc.deleted if fc.deleted is not None else '?'})"
            orig = f"  (from {fc.orig_path})" if fc.orig_path else ""
            note = f"  [{fc.note}]" if fc.note else ""
            lines.append(f"    {fc.status} {fc.path}{orig}{stat}{note}")
    if st.findings:
        blocked = sum(f.level == guard.BLOCK for f in st.findings)
        lines.append(f"  guard: {blocked} blocked, {len(st.findings) - blocked} warning(s)")
        lines.extend("  " + line for line in guard.format_findings(st.findings))
    return "\n".join(lines)
