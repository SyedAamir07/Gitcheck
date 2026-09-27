from __future__ import annotations

import os
import subprocess

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: str | None = None, check: bool = True, stdin: bytes | None = None) -> str:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
    )
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace")


def git_ok(*args: str, cwd: str | None = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def has_commit(rev: str, cwd: str | None = None) -> bool:
    return git_ok("cat-file", "-e", f"{rev}^{{commit}}", cwd=cwd)


def head_sha(cwd: str | None = None) -> str | None:
    return git("rev-parse", "HEAD", cwd=cwd).strip() if has_commit("HEAD", cwd) else None


def repo_root(cwd: str | None = None) -> str:
    return os.path.normpath(git("rev-parse", "--show-toplevel", cwd=cwd).strip())


def git_path(name: str, cwd: str | None = None) -> str:
    p = git("rev-parse", "--git-path", name, cwd=cwd).strip()
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p))


def is_repo(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


_SKIP_DIRS = {
    "node_modules", ".git", "build", "dist", "out", "target", "vendor", "venv", ".venv",
    "__pycache__", ".dart_tool", ".gradle", ".idea", ".vscode", "Pods",
}


def discover_repos(root: str, depth: int = 1) -> list[str]:
    """The root itself if it is a repo, plus repos in subfolders up to `depth` levels down.
    Does not descend into a repo found below the root."""
    root = os.path.abspath(root)
    repos = [root] if is_repo(root) else []

    def walk(folder: str, level: int) -> None:
        if level > depth:
            return
        try:
            children = sorted(os.listdir(folder))
        except OSError:
            return
        for name in children:
            full = os.path.join(folder, name)
            if name in _SKIP_DIRS or name.startswith(".") or not os.path.isdir(full):
                continue
            if is_repo(full):
                repos.append(full)
            else:
                walk(full, level + 1)

    walk(root, 1)
    return repos


def enclosing_repo(path: str) -> str | None:
    """Top-level folder of the repo that contains `path`, or None."""
    if not os.path.isdir(path):
        return None
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True)
    return os.path.normpath(r.stdout.decode("utf-8", "replace").strip()) if r.returncode == 0 else None


def find_repos(path: str, depth: int = 2) -> list[str]:
    """Repos for a user-supplied folder: the repo containing it, or the repos below it."""
    path = os.path.abspath(os.path.expanduser(path.strip().strip('"').strip("'")))
    repo = enclosing_repo(path)
    return [repo] if repo else discover_repos(path, depth)


def operation_in_progress(cwd: str | None = None) -> str | None:
    for marker, label in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ):
        if os.path.exists(git_path(marker, cwd)):
            return label
    return None


def split_z(out: str) -> list[str]:
    return [p.strip("\n") for p in out.split("\0") if p.strip("\n")]
