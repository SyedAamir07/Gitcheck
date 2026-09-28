"""Show a commit plan in full and let the user approve it, cancel it, or change it first."""
from __future__ import annotations

from typing import Callable

from . import committer
from .committer import PlannedCommit, RepoResult
from .gitops import git
from .status import FileChange, file_changes

Plan = list[tuple[str, list[PlannedCommit]]]
Ask = Callable[[str], str]
Out = Callable[[str], None]

APPROVED, CANCELLED = "approved", "cancelled"

_KINDS = (("U", "conflict"), ("D", "deleted"), ("R", "renamed"), ("C", "copied"),
          ("A", "added"), ("T", "type changed"), ("M", "modified"))


def _kind(code: str) -> str:
    if code == "??":
        return "new"
    return next((label for ch, label in _KINDS if ch in code), code.strip() or "changed")


def _describe(fc: FileChange) -> str:
    parts = [_kind(fc.status)]
    if fc.added is not None or fc.deleted is not None:
        added = "?" if fc.added is None else fc.added
        deleted = "?" if fc.deleted is None else fc.deleted
        parts.append(f"+{added} -{deleted}")
    if fc.orig_path:
        parts.append(f"from {fc.orig_path}")
    if fc.note:
        parts.append(fc.note)
    return ", ".join(parts)


def _changes_by_path(repo: str) -> dict[str, FileChange]:
    out: dict[str, FileChange] = {}
    for fc in file_changes(repo):
        out[fc.path] = fc
        if fc.orig_path:
            out.setdefault(fc.orig_path, FileChange(fc.orig_path, "D", note=f"renamed to {fc.path}"))
    return out


def dry_run(plan: Plan) -> list[RepoResult]:
    return [committer.run_repo(repo, commits, dry_run=True) for repo, commits in plan]


def render(plan: Plan, results: list[RepoResult]) -> str:
    lines: list[str] = []
    number = files_total = 0
    for (repo, commits), res in zip(plan, results):
        branch = git("branch", "--show-current", cwd=repo, check=False).strip() or "(detached)"
        changes = _changes_by_path(repo)
        lines.append(f"== {repo}  (branch: {branch})")
        if res.error:
            lines.append(f"  CANNOT COMMIT: {res.error}")
        if not commits:
            lines.append("  no commits planned")
        for pc in commits:
            number += 1
            title, _, body = pc.message.partition("\n")
            kept = [f for f in pc.files if f in changes and f not in res.excluded]
            files_total += len(kept)
            skip_note = "" if kept else "   -> will be SKIPPED, no files left to commit"
            lines.append(f"  [{number}] {title}{skip_note}")
            lines.extend(f"       | {b}" for b in body.strip("\n").splitlines())
            for i, f in enumerate(pc.files, 1):
                if f in res.excluded:
                    info = f"EXCLUDED by guard: {res.excluded[f]}"
                elif f in changes:
                    info = _describe(changes[f])
                else:
                    info = "no uncommitted changes, ignored"
                lines.append(f"       {i:>2}. {f}  ({info})")
            if not pc.files:
                lines.append("       (no files)")
        if res.warnings:
            lines.append("  Warnings (these files WILL be committed, please check):")
            lines.extend(f"       {w}" for w in res.warnings)
        if res.left_uncommitted:
            lines.append("  Left uncommitted (not in any commit):")
            lines.extend(f"       {p}" for p in res.left_uncommitted)
        lines.append("")
    lines.append(
        f"Total: {number} commit(s), {files_total} file(s) in {len(plan)} repo(s). Nothing will be pushed."
    )
    return "\n".join(lines)


def _choose(ask: Ask, out: Out, prompt: str, keys: str) -> str:
    while True:
        try:
            answer = ask(prompt).strip().lower()
        except EOFError:
            return ""
        if answer and answer[0] in keys:
            return answer[0]
        out(f"  please type one of: {', '.join(keys)}")


def _flat(plan: Plan) -> list[tuple[int, int]]:
    return [(ri, ci) for ri, (_, commits) in enumerate(plan) for ci in range(len(commits))]


def _numbers(raw: str, maximum: int) -> list[int] | None:
    tokens = raw.replace(",", " ").split()
    if not tokens or not all(t.isdigit() and 1 <= int(t) <= maximum for t in tokens):
        return None
    return sorted({int(t) for t in tokens})


def _ask_numbers(ask: Ask, out: Out, prompt: str, maximum: int) -> list[int]:
    try:
        raw = ask(f"{prompt} [1-{maximum}, several allowed like 1,3; empty = back]: ")
    except EOFError:
        return []
    if not raw.strip():
        return []
    picked = _numbers(raw, maximum)
    if picked is None:
        out("  not a valid number, nothing changed")
        return []
    return picked


def _pick_commit(plan: Plan, ask: Ask, out: Out, prompt: str) -> tuple[int, int] | None:
    flat = _flat(plan)
    try:
        raw = ask(f"{prompt} [1-{len(flat)}, empty = back]: ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    picked = _numbers(raw, len(flat))
    if not picked or len(picked) != 1:
        out("  not a valid commit number, nothing changed")
        return None
    return flat[picked[0] - 1]


def _drop_if_empty(plan: Plan, ri: int, ci: int, out: Out) -> None:
    commits = plan[ri][1]
    if not commits[ci].files:
        out(f"  commit \"{commits[ci].message.splitlines()[0]}\" has no files left, removed it")
        del commits[ci]


def _pick_files(plan: Plan, ri: int, ci: int, ask: Ask, out: Out, verb: str) -> list[str]:
    files = plan[ri][1][ci].files
    if not files:
        out("  this commit has no files")
        return []
    for i, f in enumerate(files, 1):
        out(f"    {i:>2}. {f}")
    return [files[n - 1] for n in _ask_numbers(ask, out, f"File number(s) to {verb}", len(files))]


def _remove_files(plan: Plan, ask: Ask, out: Out) -> None:
    picked = _pick_commit(plan, ask, out, "Remove files from which commit?")
    if not picked:
        return
    ri, ci = picked
    gone = _pick_files(plan, ri, ci, ask, out, "remove")
    pc = plan[ri][1][ci]
    pc.files = [f for f in pc.files if f not in gone]
    for f in gone:
        out(f"  removed {f} (it stays uncommitted in your folder)")
    _drop_if_empty(plan, ri, ci, out)


def _skip_commits(plan: Plan, ask: Ask, out: Out) -> None:
    flat = _flat(plan)
    for n in reversed(_ask_numbers(ask, out, "Skip which commit(s)?", len(flat))):
        ri, ci = flat[n - 1]
        out(f"  skipped [{n}] {plan[ri][1][ci].message.splitlines()[0]} (its files stay uncommitted)")
        del plan[ri][1][ci]


def _edit_message(plan: Plan, ask: Ask, out: Out) -> None:
    picked = _pick_commit(plan, ask, out, "Change the message of which commit?")
    if not picked:
        return
    pc = plan[picked[0]][1][picked[1]]
    title, sep, body = pc.message.partition("\n")
    out(f"  current title: {title}")
    try:
        new_title = ask("  new title (empty = keep): ").strip()
        new_body = ask("  new body (empty = keep, '-' = remove the body): ").strip()
    except EOFError:
        return
    title = new_title or title
    if new_body == "-":
        body = ""
    elif new_body:
        body = "\n" + new_body.replace("\\n", "\n")
    pc.message = f"{title}\n{body}" if body else title
    out(f"  message is now: {title}")


def _move_files(plan: Plan, ask: Ask, out: Out) -> None:
    picked = _pick_commit(plan, ask, out, "Move files out of which commit?")
    if not picked:
        return
    ri, ci = picked
    moving = _pick_files(plan, ri, ci, ask, out, "move")
    if not moving:
        return
    target = _pick_commit(plan, ask, out, "Move them into which commit?")
    if not target or target == picked:
        out("  nothing moved")
        return
    if target[0] != ri:
        out("  that commit is in a different repo, nothing moved")
        return
    src, dst = plan[ri][1][ci], plan[ri][1][target[1]]
    src.files = [f for f in src.files if f not in moving]
    dst.files.extend(moving)
    out(f"  moved {len(moving)} file(s) to \"{dst.message.splitlines()[0]}\"")
    _drop_if_empty(plan, ri, ci, out)


_ACTIONS = {"f": _remove_files, "s": _skip_commits, "m": _edit_message, "v": _move_files}


def _change(plan: Plan, ask: Ask, out: Out) -> None:
    out(
        "\nWhat do you want to change?\n"
        "  [f] remove file(s) from a commit (they stay uncommitted)\n"
        "  [s] skip whole commit(s)\n"
        "  [m] change a commit message\n"
        "  [v] move file(s) to another commit\n"
        "  [b] back"
    )
    key = _choose(ask, out, "Choice: ", "fsmvb")
    if key in _ACTIONS:
        _ACTIONS[key](plan, ask, out)


def review(plan: Plan, ask: Ask = input, out: Out = print) -> tuple[str, Plan]:
    """Loop: show every commit (title, body, files) -> yes / no / other. `other` edits the plan
    in memory and shows it again. Returns (APPROVED | CANCELLED, final plan)."""
    while True:
        out(render(plan, dry_run(plan)))
        if not any(commits for _, commits in plan):
            out("Nothing left to commit.")
            return CANCELLED, plan
        key = _choose(ask, out, "\nCommit all of this?  [y] yes   [n] no   [o] other (change something): ", "yno")
        if key == "y":
            return APPROVED, plan
        if key != "o":
            return CANCELLED, plan
        _change(plan, ask, out)
        out("")
