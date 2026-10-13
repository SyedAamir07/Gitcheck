"""Execute a commit plan: one commit per task, guard before and after every commit.

Plan file (JSON):
{
  "repos": [
    {
      "path": "D:/Project/backend",
      "commits": [
        {"message": "feat(api): read the API url from config\\n\\nOptional body",
         "files": ["src/config.ts", "src/detect.ts"],
         "allow_local": {"src/detect.ts": "line 12 is the check that rejects loopback hosts in user input, not an address the app uses"},
         "server_reason": "only needed when the message sounds local-only: why these files belong on the server"}
      ]
    }
  ]
}

Local-only files are never committed: every BLOCK finding, and every local-only WARN finding
unless `allow_local` gives evidence for that file (detection logic or test data). A commit whose
message sounds local-only (or that holds files from such a commit, even after rewording) is
skipped unless it has `server_reason`.

Per repo:
  1. The index is cleared (working tree is never touched) and the original index is saved.
  2. For each commit: stage its files, run the guard on the index, unstage every blocked or
     local-only file, commit what remains (git hooks still run).
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

from . import flags, guard
from .gitops import GitError, git, head_sha, hide_locally, operation_in_progress
from .status import porcelain

_ADD_CHUNK = 100


@dataclass
class PlannedCommit:
    message: str
    files: list[str]
    allow_local: dict[str, str] = field(default_factory=dict)  # path -> evidence the local warning is not a real value
    server_reason: str = ""  # why a commit flagged as local-only still belongs on the server


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
    excluded: dict[str, str] = field(default_factory=dict)  # blocked for other reasons (secrets, junk, build output)
    local_only: dict[str, str] = field(default_factory=dict)  # only works on this computer, never committed
    local_allowed: dict[str, str] = field(default_factory=dict)  # local warning dismissed with evidence from the plan
    flagged: dict[str, str] = field(default_factory=dict)  # commit subject -> why it looks local-only
    left_uncommitted: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
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
            allow = c.get("allow_local") or {}
            if not isinstance(allow, dict):
                raise PlanError(f"{path}: allow_local must be an object of path -> evidence")
            allow = {_norm(path, p): str(why).strip() for p, why in allow.items() if str(why).strip()}
            commits.append(PlannedCommit(msg, files, allow, str(c.get("server_reason") or "").strip()))
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


def _describe(f: guard.Finding) -> str:
    return f.reason + (f" (line {f.line}: {f.snippet})" if f.line else "")


def _judge(findings: list[guard.Finding], allow: dict[str, str],
           result: RepoResult | None) -> tuple[dict[str, str], dict[str, str]]:
    """Files that must not be committed: (blocked for other reasons, local-only). A local-only
    WARN is let through only when `allow` gives evidence for that file; a BLOCK never is.
    Records warnings and dismissed local warnings on `result`."""
    blocked: dict[str, str] = {}
    local: dict[str, str] = {}
    for f in sorted(findings, key=lambda f: f.level != guard.BLOCK):
        if f.local and (f.level == guard.BLOCK or f.path not in allow):
            local.setdefault(f.path, _describe(f))
        elif not f.local and f.level == guard.BLOCK:
            blocked.setdefault(f.path, _describe(f))
    for p in blocked:
        local.pop(p, None)
    if result is None:
        return blocked, local
    for f in findings:
        if f.level != guard.WARN or f.path in blocked or f.path in local:
            continue
        if f.local:
            result.local_allowed.setdefault(f.path, f"{allow[f.path]}  [warning was: {_describe(f)}]")
        else:
            loc = f"{f.path}:{f.line}" if f.line else f.path
            result.warnings.append(f"{loc} -- {f.reason}" + (f" | {f.snippet}" if f.snippet else ""))
    for p in sorted(set(allow) & set(local)):
        result.warnings.append(f"{p} -- allow_local ignored: this file has a BLOCK local-only finding"
                               f" (only {guard.ALLOW_FILE} or '{guard.INLINE_ALLOW}' on the line can let it through)")
    return blocked, local


def _flag_skip(repo: str, pc: PlannedCommit, subject: str, result: RepoResult) -> bool:
    """True when the commit must be skipped because it looks local-only and has no server_reason."""
    flag = flags.check(repo, pc.message, pc.files)
    if not flag:
        return False
    if pc.server_reason:
        result.flagged[subject] = f"{flag}; kept because: {pc.server_reason}"
        return False
    result.flagged[subject] = flag
    result.skipped.append(f"{subject} (flagged local-only: {flag} - re-check its files and leave the local ones"
                          " out, or give server_reason in the plan)")
    return True


def _attempt(
    repo: str,
    commits: list[PlannedCommit],
    changed: set[str],
    exclude: dict[str, str],
    local: dict[str, str],
    start: str | None,
    result: RepoResult,
) -> None:
    _unstage_all(repo, start)
    result.commits.clear()
    result.skipped.clear()
    result.local_allowed.clear()
    for pc in commits:
        subject = pc.message.splitlines()[0]
        files = [f for f in pc.files if f in changed and f not in exclude and f not in local]
        if not files:
            if not _flag_skip(repo, pc, subject, result):
                result.skipped.append(f"{subject} (no files left to commit)")
            continue
        for i in range(0, len(files), _ADD_CHUNK):
            git("add", "-A", "--", *files[i : i + _ADD_CHUNK], cwd=repo)

        findings = guard.run_checks(repo, guard.collect_staged(repo))
        blocked, local_now = _judge(findings, pc.allow_local, result)
        for path in set(blocked) | set(local_now):
            _unstage(repo, path, start)
        exclude.update(blocked)
        local.update(local_now)

        staged = _staged_paths(repo)
        if _flag_skip(repo, pc, subject, result):
            for path in staged:
                _unstage(repo, path, start)
            continue
        if not staged:
            result.skipped.append(f"{subject} (every file was left out by the guard)")
            continue
        sha = _commit(repo, pc.message)
        result.commits.append(CommitResult(sha, subject, staged))


def _dry_run(repo: str, commits: list[PlannedCommit], changed: set[str], result: RepoResult) -> None:
    worktree = guard.collect_worktree(repo)
    for pc in commits:
        subject = pc.message.splitlines()[0]
        files = [f for f in pc.files if f in changed]
        subset = {f: worktree[f] for f in files if f in worktree}
        blocked, local = _judge(guard.run_checks(repo, subset), pc.allow_local, result)
        result.excluded.update(blocked)
        result.local_only.update(local)
        kept = [f for f in files if f not in blocked and f not in local]
        if _flag_skip(repo, pc, subject, result):
            continue
        if kept:
            result.commits.append(CommitResult("(dry-run)", subject, kept))
        else:
            result.skipped.append(f"{subject} (no files left to commit)")


def run_repo(repo: str, commits: list[PlannedCommit], dry_run: bool = False, hide_local: bool = True) -> RepoResult:
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
        local: dict[str, str] = {}
        allowed = {p: why for pc in commits for p, why in pc.allow_local.items()}
        try:
            for attempt in range(2):
                result.warnings.clear()
                _attempt(repo, commits, changed, exclude, local, start, result)
                if not result.commits:
                    break
                revs = [f"{start}..HEAD"] if start else ["HEAD"]
                blocked, local_now = _judge(guard.run_checks(repo, guard.collect_revs(repo, [revs])), allowed, None)
                if not blocked and not local_now:
                    break
                _rollback(repo, start, None)
                for path, reason in blocked.items():
                    exclude[path] = f"post-commit check: {reason}"
                for path, reason in local_now.items():
                    local[path] = f"post-commit check: {reason}"
                if attempt == 1:
                    raise GitError(f"blocked files still present after retry: {sorted(set(blocked) | set(local_now))}")
        except Exception as exc:
            _rollback(repo, start, orig_tree)
            result.ok, result.error = False, f"{exc} (repo restored to its starting state)"
            result.commits.clear()
            return result
        result.excluded = exclude
        result.local_only = local

    if hide_local:
        _hide_local_only(repo, result)
    committed = {f for c in result.commits for f in c.files}
    result.left_uncommitted = sorted(changed - committed - set(result.excluded) - set(result.local_only))
    return result


def _hide_local_only(repo: str, result: RepoResult) -> None:
    """Untracked files that only make sense on this computer are hidden via .git/info/exclude,
    whether or not they were in the plan, so a later `git add .` cannot pick them up."""
    findings = guard.run_checks(repo, guard.collect_worktree(repo))
    untracked = {fc.path for fc in porcelain(repo) if fc.status == "??"}
    local = sorted(guard.local_only_files(findings) & untracked)
    if not local:
        return
    reasons = guard.local_paths(findings)
    for p in local:
        result.local_only.setdefault(p, reasons.get(p, "local-only file"))
    result.hidden = local if result.dry_run else hide_locally(repo, local)


def run_plan(plan_path: str, dry_run: bool = False, hide_local: bool = True) -> list[RepoResult]:
    return [run_repo(repo, commits, dry_run, hide_local) for repo, commits in load_plan(plan_path)]


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
    for subject, why in r.flagged.items():
        lines.append(f"  flagged local-only: {subject}  -- {why}")
    if r.local_only:
        lines.append("  Excluded as local-only (never committed):")
        hidden = set(r.hidden)
        verb = "will be hidden" if r.dry_run else "hidden"
        for p, why in sorted(r.local_only.items()):
            note = f"  [{verb} on this computer: .git/info/exclude]" if p in hidden else ""
            lines.append(f"      {p}  -- {why}{note}")
    if r.excluded:
        lines.append("  EXCLUDED by guard (not committed):")
        lines.extend(f"      {p}  -- {why}" for p, why in sorted(r.excluded.items()))
    if r.local_allowed:
        lines.append("  local-only warning dismissed with evidence from the plan (committed):")
        lines.extend(f"      {p}  -- {why}" for p, why in sorted(r.local_allowed.items()))
    if r.warnings:
        lines.append("  warnings (committed, please review):" if not r.dry_run else "  warnings:")
        lines.extend(f"      {w}" for w in r.warnings)
    if r.left_uncommitted:
        lines.append("  left uncommitted (not in plan):")
        lines.extend(f"      {p}" for p in r.left_uncommitted)
    return "\n".join(lines)
