"""Execute a commit plan: one commit per task, guard before and after every commit.

Plan file (JSON):
{
  "repos": [
    {
      "path": "D:/Project/backend",
      "commits": [
        {"message": "feat(docker): add dev image\\n\\nOptional body", "files": ["Dockerfile.dev", "docker-entrypoint.dev.sh"]}
      ]
    }
  ]
}

Per repo:
  1. The index is cleared (working tree is never touched) and the original index is saved.
  2. For each commit: stage its files, run the guard on the index, unstage every blocked
     file, commit what remains (git hooks still run).
  3. Guard the new commits again. If anything slipped through, reset --soft to the
     starting HEAD, exclude those files and redo the commits once.
  4. On any failure the repo is restored to its starting HEAD and original index.
Nothing is ever pushed.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field

from . import guard
from .gitops import GitError, git, head_sha, operation_in_progress
from .status import porcelain

_ADD_CHUNK = 100


@dataclass
class PlannedCommit:
    message: str
    files: list[str]


@dataclass
class CommitResult:
    sha: str
    subject: str
    files: list[str]


@dataclass
class RepoResult:
    path: str
    ok: bool = True
    dry_run: bool = False
    commits: list[CommitResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    left_uncommitted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class PlanError(ValueError):
    pass


def _norm(repo: str, path: str) -> str:
    p = path.strip().replace("\\", "/")
    if os.path.isabs(p):
        p = os.path.relpath(p, repo).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def load_plan(plan_path: str) -> list[tuple[str, list[PlannedCommit]]]:
    with open(plan_path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list) or not repos:
        raise PlanError("plan must be an object with a non-empty 'repos' list")
    out = []
    for r in repos:
        path = os.path.abspath(r.get("path", ""))
        if not os.path.isdir(path):
            raise PlanError(f"repo path does not exist: {r.get('path')!r}")
        commits = []
        seen: set[str] = set()
        for c in r.get("commits", []):
            msg = (c.get("message") or "").strip()
            if not msg:
                raise PlanError(f"{path}: every commit needs a message")
            files = [_norm(path, f) for f in c.get("files", []) if f.strip()]
            dup = seen.intersection(files)
            if dup:
                raise PlanError(f"{path}: file(s) listed in more than one commit: {sorted(dup)}")
            seen.update(files)
            commits.append(PlannedCommit(msg, files))
        out.append((path, commits))
    return out


def _changed_paths(repo: str) -> set[str]:
    paths: set[str] = set()
    for fc in porcelain(repo):
        paths.add(fc.path)
        if fc.orig_path:
            paths.add(fc.orig_path)
    return paths


def _staged_paths(repo: str) -> list[str]:
    return [p for p in git("diff", "--cached", "--name-only", "--no-renames", "-z", cwd=repo).split("\0") if p]


def _unstage_all(repo: str, start: str | None) -> None:
    if start:
        git("reset", "-q", cwd=repo)
    else:
        git("read-tree", "--empty", cwd=repo)


def _unstage(repo: str, path: str, start: str | None) -> None:
    if start:
        git("reset", "-q", "HEAD", "--", path, cwd=repo, check=False)
    else:
        git("rm", "-q", "--cached", "--ignore-unmatch", "--", path, cwd=repo, check=False)


def _rollback(repo: str, start: str | None, orig_tree: str | None) -> None:
    if start:
        git("reset", "-q", "--soft", start, cwd=repo)
    elif head_sha(repo):
        branch = git("symbolic-ref", "-q", "HEAD", cwd=repo).strip()
        git("update-ref", "-d", branch, cwd=repo)
    if orig_tree:
        git("read-tree", orig_tree, cwd=repo)
    else:
        _unstage_all(repo, start)


def _commit(repo: str, message: str) -> str:
    fd, tmp = tempfile.mkstemp(prefix="gitcheck-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(message.rstrip() + "\n")
        git("commit", "-q", "-F", tmp, cwd=repo)
    finally:
        os.remove(tmp)
    return git("rev-parse", "--short", "HEAD", cwd=repo).strip()


def _attempt(
    repo: str,
    commits: list[PlannedCommit],
    changed: set[str],
    exclude: dict[str, str],
    start: str | None,
    result: RepoResult,
) -> None:
    _unstage_all(repo, start)
    result.commits.clear()
    result.skipped.clear()
    for pc in commits:
        subject = pc.message.splitlines()[0]
        files = [f for f in pc.files if f in changed and f not in exclude]
        if not files:
            result.skipped.append(f"{subject} (no files left to commit)")
            continue
        for i in range(0, len(files), _ADD_CHUNK):
            git("add", "-A", "--", *files[i : i + _ADD_CHUNK], cwd=repo)

        findings = guard.run_checks(repo, guard.collect_staged(repo))
        for path, reason in guard.blocked_paths(findings).items():
            _unstage(repo, path, start)
            exclude[path] = reason
        for f in findings:
            if f.level == guard.WARN and f.path not in exclude:
                loc = f"{f.path}:{f.line}" if f.line else f.path
                result.warnings.append(f"{loc} -- {f.reason}" + (f" | {f.snippet}" if f.snippet else ""))

        staged = _staged_paths(repo)
        if not staged:
            result.skipped.append(f"{subject} (every file was excluded by the guard)")
            continue
        sha = _commit(repo, pc.message)
        result.commits.append(CommitResult(sha, subject, staged))


def _dry_run(repo: str, commits: list[PlannedCommit], changed: set[str], result: RepoResult) -> None:
    worktree = guard.collect_worktree(repo)
    for pc in commits:
        subject = pc.message.splitlines()[0]
        files = [f for f in pc.files if f in changed]
        subset = {f: worktree[f] for f in files if f in worktree}
        findings = guard.run_checks(repo, subset)
        blocked = guard.blocked_paths(findings)
        result.excluded.update(blocked)
        for f in findings:
            if f.level == guard.WARN and f.path not in blocked:
                loc = f"{f.path}:{f.line}" if f.line else f.path
                result.warnings.append(f"{loc} -- {f.reason}" + (f" | {f.snippet}" if f.snippet else ""))
        kept = [f for f in files if f not in blocked]
        if kept:
            result.commits.append(CommitResult("(dry-run)", subject, kept))
        else:
            result.skipped.append(f"{subject} (no files left to commit)")


def run_repo(repo: str, commits: list[PlannedCommit], dry_run: bool = False) -> RepoResult:
    result = RepoResult(path=repo, dry_run=dry_run)
    op = operation_in_progress(repo)
    if op:
        result.ok, result.error = False, f"{op} in progress - finish or abort it first"
        return result

    changed = _changed_paths(repo)
    planned = {f for pc in commits for f in pc.files}
    for f in sorted(planned - changed):
        result.warnings.append(f"{f} -- listed in the plan but has no uncommitted changes, ignored")

    if dry_run:
        _dry_run(repo, commits, changed, result)
    else:
        start = head_sha(repo)
        orig_tree = git("write-tree", cwd=repo, check=False).strip() or None
        exclude: dict[str, str] = {}
        try:
            for attempt in range(2):
                result.warnings.clear()
                _attempt(repo, commits, changed, exclude, start, result)
                if not result.commits:
                    break
                revs = [f"{start}..HEAD"] if start else ["HEAD"]
                leaked = guard.blocked_paths(guard.run_checks(repo, guard.collect_revs(repo, [revs])))
                if not leaked:
                    break
                _rollback(repo, start, None)
                for path, reason in leaked.items():
                    exclude[path] = f"post-commit check: {reason}"
                if attempt == 1:
                    raise GitError(f"blocked files still present after retry: {sorted(leaked)}")
        except Exception as exc:
            _rollback(repo, start, orig_tree)
            result.ok, result.error = False, f"{exc} (repo restored to its starting state)"
            result.commits.clear()
            return result
        result.excluded = exclude

    committed = {f for c in result.commits for f in c.files}
    result.left_uncommitted = sorted(changed - committed - set(result.excluded))
    return result


def run_plan(plan_path: str, dry_run: bool = False) -> list[RepoResult]:
    return [run_repo(repo, commits, dry_run) for repo, commits in load_plan(plan_path)]


def to_dict(r: RepoResult) -> dict:
    return asdict(r)


def format_result(r: RepoResult) -> str:
    title = "DRY RUN " if r.dry_run else ""
    lines = [f"== {title}{r.path}"]
    if r.error:
        lines.append(f"  FAILED: {r.error}")
    for c in r.commits:
        lines.append(f"  {c.sha}  {c.subject}")
        lines.extend(f"      {f}" for f in c.files)
    for s in r.skipped:
        lines.append(f"  skipped: {s}")
    if r.excluded:
        lines.append("  EXCLUDED by guard (not committed):")
        lines.extend(f"      {p}  -- {why}" for p, why in sorted(r.excluded.items()))
    if r.warnings:
        lines.append("  warnings (committed, please review):" if not r.dry_run else "  warnings:")
        lines.extend(f"      {w}" for w in r.warnings)
    if r.left_uncommitted:
        lines.append("  left uncommitted (not in plan):")
        lines.extend(f"      {p}" for p in r.left_uncommitted)
    return "\n".join(lines)
