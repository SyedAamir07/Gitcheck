"""Detect files and content that must never reach the remote: env files, secrets,
logs, localhost URLs in code, build output, and similar.

Escape hatches per repo:
  .commitguard-allow    one glob per line at the repo root; matching paths are skipped
  "commitguard: allow"  put this text on a line to skip content checks for that line
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

from .gitops import EMPTY_TREE, git, has_commit, split_z

ZERO_SHA = "0" * 40
ALLOW_FILE = ".commitguard-allow"
INLINE_ALLOW = "commitguard: allow"
MAX_UNTRACKED_BYTES = 1_000_000
MAX_CONTENT_FINDINGS_PER_FILE = 10

BLOCK, WARN = "BLOCK", "WARN"

TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

PATH_RULES: list[tuple[str, str, str]] = [
    (BLOCK, ".env", "environment file"),
    (BLOCK, ".env.*", "environment file"),
    (BLOCK, "*.env", "environment file"),
    (BLOCK, "*.log", "log file"),
    (BLOCK, "*.pem", "private key / certificate"),
    (BLOCK, "*.key", "private key"),
    (BLOCK, "*.p12", "keystore"),
    (BLOCK, "*.pfx", "keystore"),
    (BLOCK, "*.jks", "Android signing keystore"),
    (BLOCK, "*.keystore", "Android signing keystore"),
    (BLOCK, "key.properties", "Android signing config"),
    (BLOCK, "*firebase-adminsdk*.json", "Firebase service account (contains private key)"),
    (BLOCK, "*service-account*.json", "service account credentials"),
    (BLOCK, "*service_account*.json", "service account credentials"),
    (BLOCK, "id_rsa*", "SSH private key"),
    (BLOCK, "id_ed25519*", "SSH private key"),
    (BLOCK, ".DS_Store", "OS junk file"),
    (BLOCK, "Thumbs.db", "OS junk file"),
    (WARN, "google-services.json", "Firebase client config - confirm it belongs in the repo"),
    (WARN, "GoogleService-Info.plist", "Firebase client config - confirm it belongs in the repo"),
    (WARN, "*.local", "local-only config"),
    (WARN, "*.local.*", "local-only config"),
    (WARN, "*_err.txt", "debug/scratch output"),
    (WARN, "*error*.txt", "debug/scratch output"),
    (WARN, "*_report.txt", "debug/scratch output"),
    (WARN, "*.bak", "backup file"),
    (WARN, "*.orig", "merge leftover"),
    (WARN, "*.tmp", "temporary file"),
]

BLOCK_DIR_ANYWHERE = {
    "node_modules": "dependencies folder",
    ".dart_tool": "Dart tool cache",
    "__pycache__": "Python cache",
    ".pytest_cache": "pytest cache",
    ".gradle": "Gradle cache",
}
BLOCK_DIR_AT_ROOT = {"build": "build output", "dist": "build output", "coverage": "coverage output"}
WARN_DIR_ANYWHERE = {".idea": "IDE settings"}
WARN_DIR_AT_ROOT = {
    "tmp": "temporary files",
    "uploads": "uploaded user files",
    "private-uploads": "uploaded user files",
    "scratch": "scratch files",
}

CODE_EXTS = {
    ".dart", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".kt", ".kts",
    ".java", ".swift", ".go", ".rb", ".php", ".cs",
}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "e2e", "integration_test", "newman", "postman"}

_LOCAL_HOST = (
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|10\.0\.2\.2"
    r"|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})"
)
LOCAL_URL_RE = re.compile(r"\b(?:https?|wss?)://" + _LOCAL_HOST + r"(?::\d+)?[^\s'\"`)]*", re.I)
LOCAL_HOST_RE = re.compile(r"(?<![\w.])(?:localhost|127\.0\.0\.1|10\.0\.2\.2)(?!\w)", re.I)

SECRET_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (BLOCK, re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (BLOCK, re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (BLOCK, re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}"), "Stripe live secret key"),
    (BLOCK, re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"), "Slack token"),
    (BLOCK, re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b"), "GitHub token"),
    (WARN, re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    (
        WARN,
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
            r"\w*\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
        ),
        "hardcoded credential",
    ),
]


@dataclass
class Finding:
    level: str
    path: str
    reason: str
    line: int | None = None
    snippet: str = ""
    commits: list[str] = field(default_factory=list)


@dataclass
class Change:
    added: list[tuple[int, str, str]] = field(default_factory=list)  # (line, text, commit)
    commits: set[str] = field(default_factory=set)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_COMMIT_MARK = "@@commit "
_DIFF_OPTS = ["-U0", "--no-color", "--no-ext-diff", "--no-prefix", "--diff-filter=ACMR"]


def _parse_patch(text: str, changes: dict[str, Change]) -> None:
    path: str | None = None
    commit = ""
    lineno = 0
    for raw in text.splitlines():
        if raw.startswith(_COMMIT_MARK):
            commit, path = raw[len(_COMMIT_MARK):].strip(), None
        elif raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ "):
            target = raw[4:].rstrip("\t")
            path = None if target == "/dev/null" else target
            if path:
                ch = changes.setdefault(path, Change())
                if commit:
                    ch.commits.add(commit)
        elif raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            lineno = int(m.group(1)) if m else 0
        elif path is not None and raw.startswith("+"):
            changes[path].added.append((lineno, raw[1:], commit))
            lineno += 1


def collect_staged(repo: str) -> dict[str, Change]:
    changes: dict[str, Change] = {}
    for p in split_z(git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", cwd=repo)):
        changes.setdefault(p, Change())
    _parse_patch(git("diff", "--cached", *_DIFF_OPTS, cwd=repo), changes)
    return changes


def _read_untracked(repo: str, path: str) -> list[tuple[int, str, str]]:
    full = os.path.join(repo, path)
    try:
        if os.path.getsize(full) > MAX_UNTRACKED_BYTES:
            return []
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    if b"\0" in data[:8192]:
        return []
    return [(i, text, "") for i, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1)]


def collect_worktree(repo: str) -> dict[str, Change]:
    """Every uncommitted change: staged + unstaged + untracked (respecting .gitignore)."""
    base = "HEAD" if has_commit("HEAD", repo) else EMPTY_TREE
    changes: dict[str, Change] = {}
    for p in split_z(git("diff", base, "--name-only", "-z", "--diff-filter=ACMR", cwd=repo)):
        changes.setdefault(p, Change())
    _parse_patch(git("diff", base, *_DIFF_OPTS, cwd=repo), changes)
    for p in split_z(git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo)):
        changes.setdefault(p, Change()).added.extend(_read_untracked(repo, p))
    return changes


def collect_revs(repo: str, rev_sets: list[list[str]]) -> dict[str, Change]:
    """Files and added lines introduced by the commits selected by each `git log` rev set."""
    changes: dict[str, Change] = {}
    for revs in rev_sets:
        names = git("log", f"--format={_COMMIT_MARK}%h", "--name-only", "--diff-filter=ACMR", *revs, cwd=repo)
        commit = ""
        for line in names.splitlines():
            if line.startswith(_COMMIT_MARK):
                commit = line[len(_COMMIT_MARK):].strip()
            elif line.strip():
                changes.setdefault(line.strip(), Change()).commits.add(commit)
        _parse_patch(git("log", "-p", f"--format={_COMMIT_MARK}%h", *_DIFF_OPTS, *revs, cwd=repo), changes)
    return changes


def unpushed_rev_sets() -> list[list[str]]:
    return [["HEAD", "--not", "--remotes"]]


def pre_push_rev_sets(repo: str, stdin_text: str) -> list[list[str]]:
    rev_sets = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue
        if remote_sha != ZERO_SHA and has_commit(remote_sha, repo):
            rev_sets.append([f"{remote_sha}..{local_sha}"])
        else:
            rev_sets.append([local_sha, "--not", "--remotes"])
    return rev_sets


def load_allowlist(repo: str) -> list[str]:
    try:
        with open(os.path.join(repo, ALLOW_FILE), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []


def is_allowed(path: str, allow: list[str]) -> bool:
    p = path.lower()
    for pat in allow:
        pat = pat.lower().lstrip("/")
        if pat.endswith("/") and (p.startswith(pat) or f"/{pat}" in f"/{p}"):
            return True
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
            return True
    return False


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(seg in TEST_DIRS for seg in parts[:-1])
        or "_test." in name
        or ".test." in name
        or ".spec." in name
        or name.startswith("test_")
    )


def _redact(value: str) -> str:
    return value[:6] + "..." if len(value) > 8 else "***"


def check_path(path: str) -> list[Finding]:
    findings = []
    parts = path.split("/")
    name = parts[-1].lower()
    dirs = [d.lower() for d in parts[:-1]]

    for seg in dirs:
        if seg in BLOCK_DIR_ANYWHERE:
            findings.append(Finding(BLOCK, path, BLOCK_DIR_ANYWHERE[seg]))
            break
        if seg in WARN_DIR_ANYWHERE:
            findings.append(Finding(WARN, path, WARN_DIR_ANYWHERE[seg]))
            break
    if dirs:
        if dirs[0] in BLOCK_DIR_AT_ROOT:
            findings.append(Finding(BLOCK, path, BLOCK_DIR_AT_ROOT[dirs[0]]))
        elif dirs[0] in WARN_DIR_AT_ROOT:
            findings.append(Finding(WARN, path, WARN_DIR_AT_ROOT[dirs[0]]))

    if not name.endswith(TEMPLATE_SUFFIXES):
        for level, pattern, reason in PATH_RULES:
            if fnmatch.fnmatch(name, pattern.lower()):
                findings.append(Finding(level, path, reason))
                break
    return findings


def check_content(path: str, change: Change) -> list[Finding]:
    findings: list[Finding] = []
    ext = os.path.splitext(path)[1].lower()
    is_test = is_test_path(path)
    in_code = ext in CODE_EXTS and not is_test

    for lineno, text, commit in change.added:
        if len(findings) >= MAX_CONTENT_FINDINGS_PER_FILE:
            break
        if INLINE_ALLOW in text:
            continue
        commits = [commit] if commit else []

        for level, rx, reason in SECRET_RULES:
            m = rx.search(text)
            if m:
                findings.append(Finding(level, path, reason, lineno, _redact(m.group(0)), commits))
                break

        if is_test:
            continue
        m = LOCAL_URL_RE.search(text)
        if m:
            level = BLOCK if in_code else WARN
            findings.append(
                Finding(level, path, "localhost / private-network URL", lineno, m.group(0)[:100], commits)
            )
        elif in_code:
            m = LOCAL_HOST_RE.search(text)
            if m:
                findings.append(Finding(WARN, path, "localhost reference", lineno, text.strip()[:100], commits))
    return findings


def run_checks(repo: str, changes: dict[str, Change]) -> list[Finding]:
    allow = load_allowlist(repo)
    findings: list[Finding] = []
    for path in sorted(changes):
        if is_allowed(path, allow):
            continue
        change = changes[path]
        for f in check_path(path):
            f.commits = sorted(c for c in change.commits if c)
            findings.append(f)
        findings.extend(check_content(path, change))
    return findings


def blocked_paths(findings: list[Finding]) -> dict[str, str]:
    """path -> first blocking reason."""
    out: dict[str, str] = {}
    for f in findings:
        if f.level == BLOCK and f.path not in out:
            out[f.path] = f.reason + (f" (line {f.line}: {f.snippet})" if f.line else "")
    return out


def format_findings(findings: list[Finding], show_commits: bool = False) -> list[str]:
    ordered = [f for f in findings if f.level == BLOCK] + [f for f in findings if f.level == WARN]
    lines = []
    for f in ordered:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        extra = f"  | {f.snippet}" if f.snippet else ""
        commits = f"  [commits: {', '.join(f.commits)}]" if show_commits and f.commits else ""
        lines.append(f"  {f.level:<5}  {loc}  -- {f.reason}{extra}{commits}")
    return lines


def report(findings: list[Finding], mode: str) -> None:
    blocked = [f for f in findings if f.level == BLOCK]
    warns = [f for f in findings if f.level == WARN]
    if not findings:
        print("gitcheck guard: clean")
        return
    print(f"gitcheck guard: {len(blocked)} blocked, {len(warns)} warning(s)")
    for line in format_findings(findings, show_commits=mode not in ("staged", "worktree")):
        print(line)
    if not blocked:
        return
    print()
    if mode == "staged":
        print("Unstage a file:  git restore --staged -- <path>   (then add it to .gitignore)")
        print("Stop tracking a file but keep it on disk:  git rm --cached -- <path>")
    elif mode == "worktree":
        print("Do not stage these files; add them to .gitignore.")
    else:
        print("These files are inside commits that are not on the remote yet.")
        print("Rewrite those commits without the files before pushing (git reset --soft <base>, then recommit).")
    print(f"Intentional? Add the path to {ALLOW_FILE}, put '{INLINE_ALLOW}' on the line, or use --no-verify.")
