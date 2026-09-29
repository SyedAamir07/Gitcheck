"""Merge a branch into main (or another target branch) locally, after a full preview.

Preview (nothing changes): the commits and files that would land on the target, whether
conflicts are expected, guard findings on those commits, and anything that makes the merge
unsafe (uncommitted tracked changes, operation in progress, target diverged from its remote).

Merge: checkout target -> fast-forward it to its upstream if it is only behind -> merge with the
chosen strategy -> optionally delete the merged branch and push the target -> go back to the
branch the user started on. On any failure the merge is aborted, the target is reset to where
it was and the starting branch is checked out again. Git hooks always run.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field

from . import guard
from .gitops import GitError, git, git_ok, head_sha, operation_in_progress
from .status import porcelain

STRATEGIES = {
    "merge": "merge commit (keeps every commit, adds a merge commit)",
    "squash": "squash (all changes as one new commit on the target)",
    "ff": "fast-forward only (no merge commit; only possible when the target has nothing new)",
}
_MAIN_NAMES = ("main", "master", "develop")


@dataclass
class MergeOptions:
    strategy: str = "merge"
    message: str | None = None
    delete_branch: bool = False
    push: bool = False


@dataclass
class BranchCommit:
    sha: str
    subject: str
    author: str
    date: str


@dataclass
class MergeFile:
    path: str
    status: str
    added: int | None = None
    deleted: int | None = None


@dataclass
class MergePreview:
    path: str
    source: str
    target: str
    current: str
    base_rev: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    commits: list[BranchCommit] = field(default_factory=list)
    target_only_commits: int = 0
    can_fast_forward: bool = False
    files: list[MergeFile] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    conflicts_known: bool = True
    target_upstream: str | None = None
    target_behind_upstream: int = 0
    findings: list[guard.Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def nothing_to_merge(self) -> bool:
        return not self.commits


@dataclass
class MergeResult:
    path: str
    source: str
    target: str
    strategy: str
    ok: bool = True
    error: str | None = None
    conflicts: list[str] = field(default_factory=list)
    new_commits: list[str] = field(default_factory=list)
    branch_deleted: bool = False
    pushed: bool = False
    notes: list[str] = field(default_factory=list)


def _local(repo: str, branch: str) -> bool:
    return git_ok("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=repo)


def _remote(repo: str, branch: str) -> bool:
    return git_ok("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", cwd=repo)


def default_target(repo: str) -> str:
    head = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False).strip()
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for name in _MAIN_NAMES:
        if _local(repo, name) or _remote(repo, name):
            return name
    return "main"


def _count(repo: str, rng: str) -> int:
    return int(git("rev-list", "--count", rng, cwd=repo).strip() or 0)


def _upstream(repo: str, branch: str) -> str | None:
    up = git("rev-parse", "--abbrev-ref", f"{branch}@{{u}}", cwd=repo, check=False).strip()
    return up or None


def _files(repo: str, base: str, source: str) -> list[MergeFile]:
    names = git("diff", "--name-status", "--no-renames", "-z", f"{base}...{source}", cwd=repo).split("\0")
    files = [MergeFile(names[i + 1], names[i]) for i in range(0, len(names) - 1, 2) if names[i]]
    stats: dict[str, tuple[int | None, int | None]] = {}
    for item in git("diff", "--numstat", "--no-renames", "-z", f"{base}...{source}", cwd=repo).split("\0"):
        parts = item.strip("\n").split("\t")
        if len(parts) == 3:
            a, d, p = parts
            stats[p] = (None if a == "-" else int(a), None if d == "-" else int(d))
    for f in files:
        f.added, f.deleted = stats.get(f.path, (None, None))
    return files


def _predict_conflicts(repo: str, base: str, source: str) -> tuple[list[str], bool]:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "merge-tree", "--write-tree", "--name-only", "--no-messages", base, source],
        cwd=repo, capture_output=True,
    )
    if r.returncode == 0:
        return [], True
    if r.returncode == 1:
        lines = r.stdout.decode("utf-8", "replace").splitlines()[1:]
        return sorted({line.strip() for line in lines if line.strip()}), True
    return [], False


def preview(repo: str, source: str | None = None, target: str | None = None, fetch: bool = False) -> MergePreview:
    repo = os.path.abspath(repo)
    current = git("branch", "--show-current", cwd=repo).strip()
    source = source or current
    target = target or default_target(repo)
    p = MergePreview(path=repo, source=source or "(detached)", target=target, current=current or "(detached)")

    if fetch and not git_ok("fetch", "--quiet", "--prune", "origin", cwd=repo):
        p.notes.append("could not fetch from origin; using the branches as they are locally")
    if not source:
        p.problems.append("HEAD is detached; pass --source <branch>")
        return p
    if source == target:
        p.problems.append(f"you are on '{target}' itself; switch to the branch you want to merge or pass --source")
        return p
    if not _local(repo, source):
        p.problems.append(f"branch '{source}' does not exist locally")
        return p
    if _local(repo, target):
        p.base_rev = target
        p.target_upstream = _upstream(repo, target)
        if p.target_upstream:
            behind = _count(repo, f"{target}..{p.target_upstream}")
            ahead = _count(repo, f"{p.target_upstream}..{target}")
            if behind and ahead:
                p.problems.append(
                    f"local '{target}' and '{p.target_upstream}' have diverged ({ahead} local, {behind} remote "
                    f"commit(s)); sync '{target}' first"
                )
            elif behind:
                p.target_behind_upstream = behind
                p.base_rev = p.target_upstream
                p.notes.append(f"local '{target}' is {behind} commit(s) behind {p.target_upstream}; it will be fast-forwarded first")
    elif _remote(repo, target):
        p.base_rev = f"origin/{target}"
        p.target_upstream = p.base_rev
        p.notes.append(f"no local '{target}' branch yet; it will be created from origin/{target}")
    else:
        p.problems.append(f"target branch '{target}' does not exist (locally or on origin)")
        return p

    op = operation_in_progress(repo)
    if op:
        p.problems.append(f"a {op} is in progress; finish or abort it first")
    tracked = [fc.path for fc in porcelain(repo) if fc.status != "??"]
    if tracked:
        p.problems.append(
            f"{len(tracked)} uncommitted change(s) in tracked files; commit them first (/smart-commit) or stash them"
        )

    log = git("log", "--format=%h%x1f%s%x1f%an%x1f%ad", "--date=short", f"{p.base_rev}..{source}", cwd=repo)
    p.commits = [BranchCommit(*line.split("\x1f", 3)) for line in log.splitlines() if line.count("\x1f") == 3]
    if not p.commits:
        p.problems.append(f"nothing to merge: '{source}' has no commits that '{target}' does not have")
        return p
    p.target_only_commits = _count(repo, f"{source}..{p.base_rev}")
    p.can_fast_forward = p.target_only_commits == 0
    p.files = _files(repo, p.base_rev, source)
    if not p.can_fast_forward:
        p.conflicts, p.conflicts_known = _predict_conflicts(repo, p.base_rev, source)

    p.findings = guard.run_checks(repo, guard.collect_revs(repo, [[source, "--not", p.base_rev]]))
    blocked = guard.blocked_paths(p.findings)
    if blocked:
        p.problems.append(
            f"the guard blocks {len(blocked)} file(s) in the commits to merge: {', '.join(sorted(blocked))}"
        )
    return p


def default_message(p: MergePreview, strategy: str) -> str:
    if strategy == "squash":
        body = "\n".join(f"- {c.subject}" for c in reversed(p.commits))
        return f"Squash merge branch '{p.source}' into {p.target}\n\n{body}"
    return f"Merge branch '{p.source}' into {p.target}"


def check(p: MergePreview, strategy: str) -> list[str]:
    """Reasons this preview cannot be merged with this strategy."""
    problems = list(p.problems)
    if strategy not in STRATEGIES:
        problems.append(f"unknown strategy '{strategy}' (use: {', '.join(STRATEGIES)})")
    elif strategy == "ff" and p.commits and not p.can_fast_forward:
        problems.append(f"fast-forward is not possible: '{p.target}' has {p.target_only_commits} commit(s) not in '{p.source}'")
    if p.conflicts:
        problems.append(
            f"conflicts expected in {len(p.conflicts)} file(s); merge '{p.target}' into '{p.source}' and resolve them there first"
        )
    return problems


def _message_file(message: str) -> str:
    fd, tmp = tempfile.mkstemp(prefix="gitcheck-merge-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(message.rstrip() + "\n")
    return tmp


def merge(
    p: MergePreview,
    strategy: str = "merge",
    message: str | None = None,
    delete_branch: bool = False,
    push: bool = False,
) -> MergeResult:
    repo, source, target = p.path, p.source, p.target
    result = MergeResult(repo, source, target, strategy)
    problems = check(p, strategy)
    if problems:
        result.ok, result.error = False, "; ".join(problems)
        return result

    start_branch = p.current
    start_target = git("rev-parse", target, cwd=repo).strip() if _local(repo, target) else None
    message = message or default_message(p, strategy)
    msg_file = _message_file(message)
    pre = None
    try:
        if _local(repo, target):
            git("checkout", "-q", target, cwd=repo)
        else:
            git("checkout", "-q", "-b", target, "--track", f"origin/{target}", cwd=repo)
        if p.target_behind_upstream:
            git("merge", "-q", "--ff-only", p.target_upstream, cwd=repo)
        pre = head_sha(repo)

        if strategy == "ff":
            git("merge", "-q", "--ff-only", source, cwd=repo)
        elif strategy == "merge":
            git("merge", "-q", "--no-ff", "-F", msg_file, source, cwd=repo)
        else:
            git("merge", "-q", "--squash", source, cwd=repo)
            git("commit", "-q", "-F", msg_file, cwd=repo)
        result.new_commits = [
            line for line in git("log", "--format=%h %s", f"{pre}..HEAD", cwd=repo).splitlines() if line
        ]
    except GitError as exc:
        result.conflicts = [f for f in git("diff", "--name-only", "--diff-filter=U", cwd=repo, check=False).split() if f]
        _restore(repo, target, pre, start_target, start_branch)
        result.ok = False
        result.error = f"{exc} (merge aborted, '{target}' left as it was, back on '{start_branch}')"
        return result
    finally:
        os.remove(msg_file)

    if delete_branch:
        flag = "-D" if strategy == "squash" else "-d"
        if git_ok("branch", flag, source, cwd=repo):
            result.branch_deleted = True
        else:
            result.notes.append(f"could not delete branch '{source}'")

    if push:
        r = subprocess.run(["git", "push", "origin", target], cwd=repo, capture_output=True)
        result.pushed = r.returncode == 0
        if not result.pushed:
            err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
            result.notes.append(f"push failed, the merge is only local:\n{err}")

    back = start_branch if start_branch != target and _local(repo, start_branch) else None
    if back:
        if git_ok("checkout", "-q", back, cwd=repo):
            result.notes.append(f"back on '{back}'")
        else:
            result.notes.append(f"stayed on '{target}' (could not switch back to '{back}')")
    else:
        result.notes.append(f"now on '{target}'")
    return result


def _restore(repo: str, target: str, pre: str | None, start_target: str | None, start_branch: str) -> None:
    if operation_in_progress(repo) == "merge":
        git("merge", "--abort", cwd=repo, check=False)
    current = git("branch", "--show-current", cwd=repo, check=False).strip()
    if current == target:
        reset_to = start_target or pre
        if reset_to:
            git("reset", "-q", "--merge", reset_to, cwd=repo, check=False)
    if start_branch and start_branch != current:
        git("checkout", "-q", start_branch, cwd=repo, check=False)
    if start_target is None and current == target and start_branch != target:
        git("branch", "-D", target, cwd=repo, check=False)


def _kind(code: str) -> str:
    return {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}.get(code[:1], code)


def render(p: MergePreview, strategy: str, message: str | None, delete_branch: bool, push: bool) -> str:
    lines = [f"== {p.path}", f"  Merge:  {p.source}  ->  {p.target}"]
    if p.commits:
        lines.append(f"  Strategy: {STRATEGIES.get(strategy, strategy)}")
    lines.extend(f"  note: {n}" for n in p.notes)
    if p.commits:
        lines.append(f"  {len(p.commits)} commit(s) to merge:")
        lines.extend(f"     {c.sha}  {c.subject}  ({c.author}, {c.date})" for c in p.commits)
        if p.target_only_commits:
            lines.append(f"  '{p.target}' has {p.target_only_commits} commit(s) that '{p.source}' does not have")
        lines.append(f"  {len(p.files)} file(s) change on '{p.target}':")
        for f in p.files:
            stat = "binary" if f.added is None else f"+{f.added} -{f.deleted}"
            lines.append(f"     {_kind(f.status):<9} {f.path}  ({stat})")
        if not p.conflicts_known:
            lines.append("  Conflicts: could not predict (git 2.38+ needed)")
        elif p.conflicts:
            lines.append(f"  CONFLICTS expected in {len(p.conflicts)} file(s):")
            lines.extend(f"     {c}" for c in p.conflicts)
        else:
            lines.append("  Conflicts: none expected")
        if p.findings:
            lines.append("  Guard:")
            lines.extend("   " + line for line in guard.format_findings(p.findings, show_commits=True))
        msg = message or default_message(p, strategy)
        if strategy != "ff":
            lines.append("  Message:")
            lines.extend(f"     | {m}" for m in msg.splitlines())
        lines.append(
            f"  After merge: delete '{p.source}': {'yes' if delete_branch else 'no'};  "
            f"push '{p.target}' to origin: {'yes' if push else 'no'}"
        )
    problems = check(p, strategy)
    if problems:
        lines.append("  CANNOT MERGE:")
        lines.extend(f"     - {x}" for x in problems)
    return "\n".join(lines)


def format_result(r: MergeResult) -> str:
    lines = [f"== {r.path}", f"  {r.source} -> {r.target} ({r.strategy})"]
    if r.ok:
        lines.append("  MERGED")
        lines.extend(f"     {c}" for c in r.new_commits)
        if r.branch_deleted:
            lines.append(f"  deleted branch '{r.source}'")
        lines.append(f"  pushed '{r.target}' to origin" if r.pushed else "  not pushed")
    else:
        lines.append(f"  FAILED: {r.error}")
        if r.conflicts:
            lines.append("  conflicted files:")
            lines.extend(f"     {c}" for c in r.conflicts)
    lines.extend(f"  {n}" for n in r.notes)
    return "\n".join(lines)


def preview_dict(p: MergePreview, strategy: str = "merge") -> dict:
    d = asdict(p)
    d.update(ok=p.ok, nothing_to_merge=p.nothing_to_merge, merge_problems=check(p, strategy))
    return d


def result_dict(r: MergeResult) -> dict:
    return asdict(r)
