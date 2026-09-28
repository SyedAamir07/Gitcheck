"""Detect files and content that must never reach the remote: env files, secrets, logs,
build output, and anything that only works on this computer: localhost / LAN / emulator
addresses anywhere (code, config, compose, Dockerfiles, scripts, CI, defaults, fallbacks,
comments, examples), this computer's paths / name / IPs, machine and device setup, local-named
files, new run/deploy setup that nothing uses, and container builds that copy local material.

Every such finding has `local=True`. BLOCK findings are never committed; local WARN findings
are excluded by the commit step unless the plan gives evidence that the line is detection
logic or test data. Only tests are skipped; docs only warn (allowed when they explain local setup).

Escape hatches per repo:
  .commitguard-allow    one glob per line at the repo root; matching paths are skipped
  "commitguard: allow"  put this text on a line to skip content checks for that line
"""
from __future__ import annotations

import fnmatch
import functools
import ipaddress
import os
import re
import socket
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
    (BLOCK, "*.local", "local-only file (named for this computer)"),
    (BLOCK, "*.local.*", "local-only file (named for this computer)"),
    (BLOCK, "docker-compose.override.*", "local Docker override (only for this computer)"),
    (BLOCK, "compose.override.*", "local Docker override (only for this computer)"),
    (WARN, "google-services.json", "Firebase client config - confirm it belongs in the repo"),
    (WARN, "GoogleService-Info.plist", "Firebase client config - confirm it belongs in the repo"),
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
DOC_EXTS = {".md", ".mdx", ".rst", ".adoc"}
DOC_DIRS = {"docs", "doc"}
CI_PATHS = (".github/workflows/", ".gitlab-ci", ".circleci/", "azure-pipelines", "bitbucket-pipelines",
            "jenkinsfile", ".drone.yml", ".woodpecker")

_PRIVATE_172 = r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
_LOCAL_HOST = (
    r"(?:localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|\[::1\]|host\.docker\.internal"
    r"|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|" + _PRIVATE_172 +
    r"|[\w-]+\.local(?![\w.-])|[\w-]+\.localhost)"
)
LOCAL_URL_RE = re.compile(
    r"\b(?:https?|wss?|[a-z][a-z0-9+.-]*)://(?:[^\s/@'\"`]+@)?" + _LOCAL_HOST + r"(?::\d+)?[^\s'\"`)]*", re.I
)
LOCAL_HOST_RE = re.compile(r"(?<![\w.])(?:localhost|127\.0\.0\.1|10\.0\.2\.2|host\.docker\.internal)(?!\w)", re.I)
# Addresses that only exist on the developer's own network or emulator, never on a server.
DEV_NETWORK_RE = re.compile(r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.0\.[23]\.2|" + _PRIVATE_172 + r")(?![\d])")
# A connection setting assigned a bare localhost (no scheme), e.g. API_HOST=localhost:3000.
LOCAL_SETTING_RE = re.compile(
    r"[\w.-]*(?:NEXT_PUBLIC_|VITE_|REACT_APP_|EXPO_PUBLIC_|PUBLIC_|api|base[_-]?url|backend|frontend|endpoint"
    r"|server|socket|origin|webhook|callback|redirect|proxy|host)[\w.-]*['\"]?\s*[:=]\s*['\"]?"
    r"(?:localhost|127\.0\.0\.1)(?::\d+)?(?![\w.])",
    re.I,
)
DEVICE_SETUP_RE = re.compile(
    r"\badb(?:\.exe)?\s+(?:-[sd]\s+\S+\s+|-[de]\s+)?(?:reverse|forward|connect)\b"
    r"|\biproxy\s+\d+|\b(?:ngrok|localtunnel|cloudflared\s+tunnel)\b|\blt\s+--port\b",
    re.I,
)
SCRIPT_EXTS = {".bat", ".cmd", ".ps1", ".psm1", ".sh", ".bash", ".zsh", ".command"}
# Code files like local_storage.dart are ordinary app code, so these names only count outside code.
LOCAL_NAME_RE = re.compile(r"(?:^|[._-])local(?:[._-]|$)|^run[_-]?local|^start[_-]?local", re.I)
MACHINE_SCRIPT_NAME_RE = re.compile(
    r"firewall|allow[-_]?port|open[-_]?port|port[-_]?forward|portproxy|hosts[-_]?file|adb[-_]?(?:reverse|forward)", re.I
)
# Files a developer runs or reads on their own computer: scripts, notes, extension-less command files.
MACHINE_FILE_EXTS = SCRIPT_EXTS | {".txt", ""}

# Commands that change this computer or a connected device instead of the app.
MACHINE_SETUP_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (BLOCK, re.compile(r"\bnetsh(?:\.exe)?\s+(?:advfirewall|firewall|interface\s+portproxy)\b", re.I),
     "changes this computer's firewall / port rules"),
    (BLOCK, re.compile(r"\b(?:New|Set|Enable|Disable|Remove)-Net(?:Firewall\w*|Nat)\b", re.I),
     "changes this computer's firewall / port rules"),
    (BLOCK, re.compile(r"(?:>>?|Add-Content|Set-Content|Out-File|\btee\b|\bsed\s+-i\b).*(?:drivers[\\/]+etc[\\/]+hosts|/etc/hosts)\b", re.I),
     "edits this computer's hosts file"),
    (BLOCK, re.compile(r"#Requires\s+-RunAsAdministrator|-Verb\s+RunAs\b|\brun\s+(?:this\s+|it\s+)?as\s+administrator", re.I),
     "admin-only setup of this computer"),
    (BLOCK, DEVICE_SETUP_RE, "phone / emulator / tunnel setup for testing on this computer"),
    (BLOCK, re.compile(r"\bflutter\s+run\b.*\s-d\s+(?!chrome\b|web-server\b|windows\b|macos\b|linux\b)\S+|\badb\s+-s\s+\S+", re.I),
     "runs on one specific phone / emulator connected to this computer"),
    (WARN, re.compile(r"\b(?:ufw\s+allow|iptables\s+-[AI]|firewall-cmd\b)", re.I),
     "changes a machine's firewall - fine only in a server provisioning script"),
    (WARN, re.compile(r"\bSet-ExecutionPolicy\b", re.I), "changes this computer's PowerShell policy"),
    (WARN, re.compile(r"\b(?:flutter\s+run|npm\s+run\s+(?:dev|start:dev)|next\s+dev|nodemon)\b", re.I),
     "starts the app for local development - confirm the team needs this file"),
]

WIN_USER_PATH_RE = re.compile(
    r"(?<![\w/])[A-Za-z]:(?:\\\\|[\\/])Users(?:\\\\|[\\/])(?!Public\b|Default\b|All Users\b)[\w.-]+", re.I
)
MAC_USER_PATH_RE = re.compile(r"(?:^|(?<=[\s'\"`=:(\[,]))/Users/(?!Shared/)[\w.-]+/")
DRIVE_PATH_RE = re.compile(r"(?<![\w/\\])[A-Za-z]:(?:\\\\|[\\/])[\w .()-]+(?:(?:\\\\|[\\/])[\w .()-]*)*", re.I)
_GENERIC_HOSTNAMES = {"localhost", "local", "server", "desktop", "laptop", "admin", "user", "ubuntu", "debian",
                      "build", "runner", "docker", "host", "linux", "windows", "macbook"}

_LOCAL_FILE_REASONS = {
    "local-only file (named for this computer)",
    "local Docker override (only for this computer)",
    "script that sets up this computer (firewall / ports / device)",
} | {reason for level, _, reason in MACHINE_SETUP_RULES if level == BLOCK}
# Path findings that mean "this file is from this computer" (not a secret or a cache).
_LOCAL_PATH_REASONS = _LOCAL_FILE_REASONS | {
    "debug/scratch output", "backup file", "merge leftover", "temporary file", "IDE settings",
    "temporary files", "uploaded user files", "scratch files",
}

LOCAL_RULES_FILE = ".commitguard-local"
LOCAL_RULES_CONTENT_PREFIX = "content:"

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
    local_file: bool = False  # the whole file only makes sense on this computer (not just one value)
    local: bool = False  # any sign the file only works on this computer; excluded unless proven otherwise


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


def collect_tracked(repo: str) -> dict[str, Change]:
    """Every file already committed, with its whole current content (audit of what is in the repo)."""
    changes: dict[str, Change] = {}
    for p in split_z(git("ls-files", "-z", cwd=repo)):
        changes[p] = Change(added=_read_untracked(repo, p))
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
        else:
            ext = os.path.splitext(name)[1]
            if ext not in CODE_EXTS and name != LOCAL_RULES_FILE and LOCAL_NAME_RE.search(name):
                findings.append(Finding(BLOCK, path, "local-only file (named for this computer)"))
            elif ext in SCRIPT_EXTS and MACHINE_SCRIPT_NAME_RE.search(name):
                findings.append(Finding(BLOCK, path, "script that sets up this computer (firewall / ports / device)"))
    for f in findings:
        f.local_file = f.reason in _LOCAL_FILE_REASONS
        f.local = f.reason in _LOCAL_PATH_REASONS
    return findings


@dataclass
class LocalRules:
    paths: list[tuple[str, str]] = field(default_factory=list)  # (glob, rules file)
    content: list[tuple[re.Pattern[str], str, str]] = field(default_factory=list)  # (regex, text, rules file)


def global_local_rules_path() -> str:
    from . import config

    return os.path.join(os.path.dirname(config.config_path()), "local-only")


def _read_local_rules(path: str, rules: LocalRules) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(LOCAL_RULES_CONTENT_PREFIX):
            pattern = line[len(LOCAL_RULES_CONTENT_PREFIX):].strip()
            try:
                rules.content.append((re.compile(pattern, re.I), pattern, path))
            except re.error:
                continue
        else:
            rules.paths.append((line.replace("\\", "/"), path))


def load_local_rules(repo: str) -> LocalRules:
    """User rules for what is local-only: ~/.gitcheck/local-only (every repo) and
    <repo>/.commitguard-local (one repo). One path glob per line, or `content: <regex>`."""
    rules = LocalRules()
    _read_local_rules(global_local_rules_path(), rules)
    _read_local_rules(os.path.join(repo, LOCAL_RULES_FILE), rules)
    return rules


def check_local_rules(path: str, change: Change, rules: LocalRules) -> list[Finding]:
    if os.path.basename(path) in (LOCAL_RULES_FILE, ALLOW_FILE):
        return []
    for pattern, source in rules.paths:
        if is_allowed(path, [pattern]):
            return [Finding(BLOCK, path, f"your local-only rule '{pattern}' ({os.path.basename(source)})",
                            local_file=True, local=True)]
    for lineno, text, commit in change.added:
        if INLINE_ALLOW in text:
            continue
        for rx, pattern, source in rules.content:
            m = rx.search(text)
            if m:
                return [Finding(BLOCK, path, f"your local-only rule 'content: {pattern}' ({os.path.basename(source)})",
                                lineno, m.group(0)[:100], [commit] if commit else [], local=True)]
    return []


def _is_doc(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        os.path.splitext(name)[1] in DOC_EXTS
        or any(seg in DOC_DIRS for seg in parts[:-1])
        or name.startswith(("readme", "changelog", "contributing"))
    )


def _is_ci(path: str) -> bool:
    p = path.lower()
    return any(marker in p for marker in CI_PATHS)


@functools.lru_cache(maxsize=None)
def _this_computer() -> tuple[tuple[re.Pattern[str], str], ...]:
    """This computer's name and LAN IPs, worked out at runtime for whoever runs the guard."""
    found: list[tuple[re.Pattern[str], str]] = []
    host = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "").strip()
    if len(host) >= 4 and host.lower() not in _GENERIC_HOSTNAMES:
        found.append((re.compile(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", re.I), "this computer's name"))
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        ips = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private and not addr.is_loopback and not addr.is_link_local:
            found.append((re.compile(rf"(?<![\d.]){re.escape(ip)}(?!\d)"), "this computer's LAN IP"))
    return tuple(found)


def machine_identity_finding(text: str, in_code: bool) -> tuple[str, str, str] | None:
    """A path, name or IP that belongs to this computer."""
    for rx, reason in ((WIN_USER_PATH_RE, "path inside a personal Windows user folder"),
                       (MAC_USER_PATH_RE, "path inside a personal macOS user folder")):
        m = rx.search(text)
        if m:
            return BLOCK, reason, m.group(0)[:100]
    for rx, reason in _this_computer():
        m = rx.search(text)
        if m:
            return BLOCK, reason, m.group(0)[:100]
    m = DRIVE_PATH_RE.search(text)
    if m:
        return (WARN if in_code else BLOCK), "path on this computer's disk", m.group(0)[:100]
    return None


_LOCAL_URL_MSG = "points at this computer / private network - breaks on the server"
_COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|<!--|::|rem\b|--|;)", re.I)
_DEFAULT_RE = re.compile(r"\$\{[^}]*:?-|\?\?|\|\||\bor\b|\bdefault", re.I)


def _url_reason(text: str) -> str:
    if _COMMENT_RE.search(text):
        return _LOCAL_URL_MSG + " (in a comment / instruction: builds or people will use it)"
    if _DEFAULT_RE.search(text):
        return _LOCAL_URL_MSG + " (a default / fallback still ships with the app)"
    return _LOCAL_URL_MSG


def local_finding(path: str, text: str) -> tuple[str, str, str] | None:
    """(level, reason, snippet) when an added line points at this computer or its network.

    Anything local is BLOCK, including defaults, fallbacks, comments, examples and build or run
    instructions. What stays WARN still counts as local-only (Finding.local) and is excluded by
    the commit step unless the plan shows it is detection logic or test data. Docs are WARN: they
    are allowed only when they explain local setup."""
    ext = os.path.splitext(path)[1].lower()
    in_code = ext in CODE_EXTS

    if _is_doc(path):
        hit = (DEV_NETWORK_RE.search(text) or LOCAL_URL_RE.search(text) or LOCAL_HOST_RE.search(text)
               or WIN_USER_PATH_RE.search(text) or MAC_USER_PATH_RE.search(text))
        if not hit:
            hit = next((rx.search(text) for _, rx, _ in MACHINE_SETUP_RULES if rx.search(text)), None)
        if hit:
            return WARN, "local address / setup in docs - allowed only if the doc explains local setup", hit.group(0)[:100]
        return None

    dev = DEV_NETWORK_RE.search(text)
    if dev:
        return BLOCK, "LAN / emulator address - only works on your own network or emulator", dev.group(0)
    identity = machine_identity_finding(text, in_code)
    if identity:
        return identity
    if ext in MACHINE_FILE_EXTS:
        for level, rx, reason in MACHINE_SETUP_RULES:
            if rx.search(text):
                return level, reason, text.strip()[:100]

    url = LOCAL_URL_RE.search(text)
    if url:
        return BLOCK, _url_reason(text), url.group(0)[:100]
    m = LOCAL_SETTING_RE.search(text)
    if m:
        return BLOCK, _url_reason(text), m.group(0)[:100]
    if LOCAL_HOST_RE.search(text):
        return WARN, "localhost reference - allowed only if this line is detection logic or test data", text.strip()[:100]
    return None


def check_content(path: str, change: Change) -> list[Finding]:
    findings: list[Finding] = []
    skip_local = is_test_path(path)

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

        if skip_local:
            continue
        local = local_finding(path, text)
        if local:
            level, reason, snippet = local
            findings.append(Finding(level, path, reason, lineno, snippet, commits,
                                    local_file=level == BLOCK and reason in _LOCAL_FILE_REASONS, local=True))
    return findings


PIPELINE = "CI / deploy pipeline"
CONTAINER = "container image build"
_COMPOSE_NAME_RE = re.compile(r"^(?:docker-)?compose(?:[._-][\w.-]*)?\.ya?ml$")
_PROXY_NAME_RE = re.compile(r"^(?:nginx|caddy|traefik|haproxy|apache|httpd|proxy)[\w.-]*\.(?:conf|ya?ml|toml|cfg)$")
_RUN_SCRIPT_NAME_RE = re.compile(r"^(?:run|start|serve|dev|deploy|launch|up|boot)(?:[._-]|$)")
_DEPLOY_DIRS = {"k8s", "kubernetes", "helm", "charts", "deploy", "deployment", "deployments", "infra", "manifests"}
_PIPELINE_PURPOSE_RE = re.compile(r"deploy|release|publish|ship|(?<![a-z])cd(?![a-z])", re.I)


def setup_kind(path: str) -> str | None:
    """What kind of run / deploy setup a file is, or None."""
    parts = path.lower().split("/")
    name, dirs = parts[-1], parts[:-1]
    ext = os.path.splitext(name)[1]
    if _is_ci(path):
        return PIPELINE
    if name in ("dockerfile", "containerfile") or name.startswith(("dockerfile.", "containerfile.")) \
            or name.endswith((".dockerfile", ".containerfile")):
        return CONTAINER
    if _COMPOSE_NAME_RE.match(name):
        return "container compose setup"
    if name == "caddyfile" or _PROXY_NAME_RE.match(name) or (ext == ".conf" and any("nginx" in d or "proxy" in d for d in dirs)):
        return "reverse proxy config"
    if name == "procfile" or (ext in SCRIPT_EXTS and _RUN_SCRIPT_NAME_RE.match(name)):
        return "run / deploy script"
    if ext in (".yml", ".yaml", ".toml", ".tf", ".hcl") and any(d in _DEPLOY_DIRS for d in dirs):
        return "deploy manifest"
    return None


def _head_files(repo: str) -> set[str]:
    if not has_commit("HEAD", repo):
        return set()
    return set(split_z(git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=repo)))


_IMPLICIT_CONTAINER_REFS = ("docker build", "docker-compose", "docker compose", "buildx", "podman build",
                            "build: .", "context:", "docker/build-push-action")


def _referenced_by(repo: str, path: str, ignore: set[str]) -> list[str]:
    """Other files (outside tests) that mention this file, by name or, for a default-named
    container file, by the build commands that pick it up implicitly."""
    name = os.path.basename(path)
    terms = [name]
    if name.lower() in ("dockerfile", "containerfile"):
        terms += _IMPLICIT_CONTAINER_REFS
    args = ["grep", "-l", "-i", "-F", "-z", "--untracked"]
    for term in terms:
        args += ["-e", term]
    out = git(*args, cwd=repo, check=False)
    return [p for p in split_z(out) if p not in ignore and not is_test_path(p)]


def _pipeline_purpose(path: str) -> bool:
    return bool(_PIPELINE_PURPOSE_RE.search(os.path.basename(path)))


def check_new_setup(repo: str, new_paths: set[str]) -> list[Finding]:
    """New run / deploy setup must be wired into the real build or deploy and must not be a
    second copy of one that exists. Anything that fails counts as local-only."""
    findings: list[Finding] = []
    new_setup = {p: setup_kind(p) for p in new_paths if not is_test_path(p)}
    new_setup = {p: k for p, k in new_setup.items() if k}
    if not new_setup:
        return findings
    existing = {p: setup_kind(p) for p in _head_files(repo)}
    # A new pipeline or README that uses the file counts; other new setup files do not
    # (a new compose file pointing at a new Dockerfile is still an unused pair).
    new_non_pipeline = {p for p, k in new_setup.items() if k != PIPELINE}
    for path, kind in sorted(new_setup.items()):
        if kind != PIPELINE and not _referenced_by(repo, path, new_non_pipeline | {path}):
            findings.append(Finding(
                WARN, path, f"new {kind} that nothing in the project references (CI, README, deploy scripts)"
                " - looks like a local run setup", local=True))
        same = sorted(p for p, k in existing.items() if k == kind and p != path
                      and (kind != PIPELINE or (_pipeline_purpose(p) and _pipeline_purpose(path))))
        if same:
            findings.append(Finding(
                WARN, path, f"a {kind} already exists ({', '.join(same[:3])}) - may duplicate the real deploy path",
                local=True))
    return findings


_COPY_RE = re.compile(r"^\s*(?:COPY|ADD)\s+(.+)$", re.I)
_LOCAL_BUILD_DIRS = {"build", "dist", "out", ".next", ".nuxt", "target", "bin", "obj", "coverage", "node_modules"}
_ENV_IGNORE_RE = re.compile(r"^(?:\*\*/)?(?:\.env[\w.*-]*|\*\.env|\*|\*\*)$")


def _copy_sources(args: str) -> list[str]:
    args = args.strip()
    if args.startswith("["):
        items = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", args)
    else:
        items = [a for a in args.split() if not a.startswith("--")]
    return [s.strip("'\"") for s in items[:-1]]


def _dockerignore_hides_env(repo: str, path: str) -> bool:
    folder = os.path.dirname(os.path.join(repo, path))
    candidates = [os.path.join(repo, path) + ".dockerignore", os.path.join(folder, ".dockerignore"),
                  os.path.join(repo, ".dockerignore")]
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines()]
        except OSError:
            continue
        return any(_ENV_IGNORE_RE.match(ln) for ln in lines if ln and not ln.startswith(("#", "!")))
    return False


def check_container_build(repo: str, path: str, change: Change) -> list[Finding]:
    """A container build must not pull local env files, local build output or local settings in."""
    findings: list[Finding] = []
    for lineno, text, commit in change.added:
        if INLINE_ALLOW in text:
            continue
        m = _COPY_RE.match(text)
        if not m or "--from" in m.group(1).lower():
            continue
        commits = [commit] if commit else []
        for src in _copy_sources(m.group(1)):
            s = src.replace("\\", "/")
            while s.startswith("./"):
                s = s[2:]
            s = s.rstrip("/") or "."
            first, name = s.split("/")[0].lower(), s.split("/")[-1].lower()
            reason = None
            if s in (".", "*"):
                if not _dockerignore_hides_env(repo, path):
                    reason = "copies the whole project folder and no .dockerignore keeps local env files (.env*) out"
            elif re.match(r"^(?:\.env(?:\..*)?|.*\.env)$", name) and not name.endswith(TEMPLATE_SUFFIXES):
                reason = "copies a local env file into the image"
            elif first in _LOCAL_BUILD_DIRS:
                reason = "copies build output from this computer - the image must build it itself"
            elif LOCAL_NAME_RE.search(name) or name in ("local.properties", "settings.json", ".npmrc", ".pypirc"):
                reason = "copies local settings from this computer into the image"
            if reason:
                findings.append(Finding(BLOCK, path, reason, lineno, text.strip()[:100], commits, local=True))
                break
    return findings


def local_only_files(findings: list[Finding]) -> set[str]:
    """Paths whose whole file only makes sense on this computer (safe to hide locally)."""
    return {f.path for f in findings if f.level == BLOCK and f.local_file}


def run_checks(repo: str, changes: dict[str, Change]) -> list[Finding]:
    allow = load_allowlist(repo)
    local_rules = load_local_rules(repo)
    findings: list[Finding] = []
    checked = [p for p in sorted(changes) if not is_allowed(p, allow)]
    for path in checked:
        change = changes[path]
        for f in check_path(path) + check_local_rules(path, change, local_rules):
            f.commits = f.commits or sorted(c for c in change.commits if c)
            findings.append(f)
        findings.extend(check_content(path, change))
        if setup_kind(path) == CONTAINER and not is_test_path(path):
            findings.extend(check_container_build(repo, path, change))
    if any(setup_kind(p) for p in checked):
        head = _head_files(repo)
        findings.extend(check_new_setup(repo, {p for p in checked if p not in head}))
    return findings


def local_paths(findings: list[Finding]) -> dict[str, str]:
    """path -> first local-only reason (BLOCK or WARN)."""
    out: dict[str, str] = {}
    for f in sorted(findings, key=lambda f: f.level != BLOCK):
        if f.local and f.path not in out:
            out[f.path] = f.reason + (f" (line {f.line}: {f.snippet})" if f.line else "")
    return out


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
        level = "LOCAL" if f.local and f.level == WARN else f.level
        lines.append(f"  {level:<5}  {loc}  -- {f.reason}{extra}{commits}")
    return lines


def report(findings: list[Finding], mode: str) -> None:
    blocked = [f for f in findings if f.level == BLOCK]
    warns = [f for f in findings if f.level == WARN]
    if not findings:
        print("gitcheck guard: clean")
        return
    local_warns = [f for f in warns if f.local]
    print(f"gitcheck guard: {len(blocked)} blocked, {len(warns)} warning(s)"
          + (f" ({len(local_warns)} local-only)" if local_warns else ""))
    for line in format_findings(findings, show_commits=mode not in ("staged", "worktree")):
        print(line)
    if local_warns:
        print()
        print("LOCAL = only works on this computer. gitcheck commit leaves these files out unless the plan")
        print("shows the line is detection logic or test data (allow_local).")
    if not blocked:
        return
    print()
    if mode == "staged":
        print("Unstage a file:  git restore --staged -- <path>   (then add it to .gitignore)")
        print("Stop tracking a file but keep it on disk:  git rm --cached -- <path>")
    elif mode == "worktree":
        print("Do not stage these files; add them to .gitignore.")
    elif mode == "tracked":
        print("These files are already committed.")
        print("Local-only file: git rm --cached -- <path>, then add it to .gitignore (the file stays on disk).")
        print("Local value inside a needed file: replace it with an env var / build arg without a localhost default.")
    else:
        print("These files are inside commits that are not on the remote yet.")
        print("Rewrite those commits without the files before pushing (git reset --soft <base>, then recommit).")
    print(f"Intentional? Add the path to {ALLOW_FILE}, put '{INLINE_ALLOW}' on the line, or use --no-verify.")
