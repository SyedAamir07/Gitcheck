from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from . import committer, config, guard, hooks, installer, skill, status
from .gitops import GitError, discover_repos, enclosing_repo, find_repos, is_repo, repo_root


def _configured() -> list[str]:
    return [p for p in config.projects() if is_repo(p)]


def _repos(args: argparse.Namespace) -> list[str]:
    """Explicit paths > --root > --configured > current folder > configured projects."""
    if args.repos:
        found = [r for p in args.repos for r in find_repos(p, args.depth)]
        if not found:
            raise SystemExit("gitcheck: no git repos found in the given path(s)")
        return found
    if args.root:
        found = discover_repos(args.root, args.depth)
        if not found:
            raise SystemExit(f"gitcheck: no git repos found in {os.path.abspath(args.root)}")
        return found
    if args.configured:
        found = _configured()
        if not found:
            raise SystemExit("gitcheck: no projects configured. Run: python gitcheck.py setup")
        return found
    here = enclosing_repo(os.getcwd())
    found = [here] if here else discover_repos(os.getcwd(), args.depth)
    if found:
        return found
    found = _configured()
    if found:
        print("gitcheck: no repo in the current folder, using your configured projects", file=sys.stderr)
        return found
    raise SystemExit(
        "gitcheck: no git repos found here and no projects configured.\n"
        "Run:  python gitcheck.py setup   (or pass repo folders as arguments)"
    )


def cmd_setup(args: argparse.Namespace) -> int:
    return installer.setup(args.folders, args.hooks, args.yes, args.force)


def cmd_projects(args: argparse.Namespace) -> int:
    if args.action == "add":
        if not args.folders:
            raise SystemExit("gitcheck: give at least one folder")
        return installer.add(args.folders, install_hooks=not args.no_hooks, force_hooks=args.force)
    if args.action == "remove":
        removed = config.remove_projects(args.folders)
        for p in removed:
            print(f"removed: {p}  (its hooks stay; remove them with: uninstall-hooks \"{p}\")")
        if not removed:
            print("nothing removed - use the exact paths shown by: projects list")
        return 0
    projects = config.projects()
    print(f"{config.config_path()}")
    if not projects:
        print("  no projects - add with: python gitcheck.py projects add <folder>")
    for p in projects:
        mark = "" if is_repo(p) else "   (missing or not a git repo)"
        print(f"  {p}{mark}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return installer.doctor()


def cmd_uninstall(args: argparse.Namespace) -> int:
    return installer.uninstall(args.keep_config)


def cmd_status(args: argparse.Namespace) -> int:
    results = [status.repo_status(r) for r in _repos(args)]
    if not args.all:
        results = [r for r in results if r.changes or r.error or r.unpushed_commits]
    if args.json:
        print(json.dumps([status.to_dict(r) for r in results], indent=2))
    elif not results:
        print("gitcheck: nothing to commit in any repo")
    else:
        print("\n\n".join(status.format_status(r) for r in results))
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    if args.staged:
        mode, changes = "staged", guard.collect_staged(repo)
    elif args.worktree:
        mode, changes = "worktree", guard.collect_worktree(repo)
    elif args.unpushed:
        mode, changes = "unpushed", guard.collect_revs(repo, guard.unpushed_rev_sets())
    elif args.range:
        mode, changes = "range", guard.collect_revs(repo, [args.range])
    else:
        mode, changes = "pre-push", guard.collect_revs(repo, guard.pre_push_rev_sets(repo, sys.stdin.read()))
    findings = guard.run_checks(repo, changes)
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        guard.report(findings, mode)
    return 1 if any(f.level == guard.BLOCK for f in findings) else 0


def cmd_commit(args: argparse.Namespace) -> int:
    try:
        results = committer.run_plan(args.plan, dry_run=args.dry_run)
    except (committer.PlanError, json.JSONDecodeError, OSError) as exc:
        print(f"gitcheck: invalid plan: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([committer.to_dict(r) for r in results], indent=2))
    else:
        print("\n\n".join(committer.format_result(r) for r in results))
        if not args.dry_run and any(r.commits for r in results):
            print("\nNothing was pushed. Review with: git log --stat -n <count>")
    return 0 if all(r.ok for r in results) else 1


def cmd_install_hooks(args: argparse.Namespace) -> int:
    for r in _repos(args):
        print(f"== {r}")
        for note in hooks.install(r, force=args.force):
            print(f"  {note}")
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace) -> int:
    for r in _repos(args):
        print(f"== {r}")
        for note in hooks.uninstall(r):
            print(f"  {note}")
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    print(f"skill installed: {skill.install(args.target)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gitcheck",
        description="Task-aware commits across repos, with a guard against files that must never be pushed.",
        epilog="First time? Run:  python gitcheck.py setup",
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")

    def add_repo_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("repos", nargs="*", metavar="PATH", help="repo or parent folders (default: see below)")
        p.add_argument("--root", help="scan this folder for repos")
        p.add_argument("--configured", action="store_true", help="use the projects saved by setup")
        p.add_argument("--depth", type=int, default=2, help="how many folder levels to scan (default 2)")
        p.epilog = (
            "Repo selection: PATH arguments, else --root, else --configured, else the repo/repos in the "
            "current folder, else the projects saved by setup."
        )

    p = sub.add_parser("setup", help="first-time setup: choose project folders, install skill and hooks")
    p.add_argument("folders", nargs="*", help="project folders (asked interactively if omitted)")
    hg = p.add_mutually_exclusive_group()
    hg.add_argument("--hooks", dest="hooks", action="store_true", default=None, help="install hooks without asking")
    hg.add_argument("--no-hooks", dest="hooks", action="store_false", help="do not install hooks")
    p.add_argument("--yes", "-y", action="store_true", help="no questions, use defaults")
    p.add_argument("--force", action="store_true", help="install hooks even where core.hooksPath is set")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("projects", help="list / add / remove your saved project folders")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    p.add_argument("folders", nargs="*")
    p.add_argument("--no-hooks", action="store_true", help="add without installing hooks")
    p.add_argument("--force", action="store_true", help="install hooks even where core.hooksPath is set")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("doctor", help="check that python, git, skill and hooks are set up correctly")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("uninstall", help="remove the skill, hooks from saved projects, and the config")
    p.add_argument("--keep-config", action="store_true", help="keep the saved project list")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="show uncommitted changes and guard findings for every repo")
    add_repo_args(p)
    p.add_argument("--all", action="store_true", help="include repos with nothing to commit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("guard", help="check for files/content that must not be committed or pushed")
    p.add_argument("-C", dest="repo", default=".", help="repo path (default: current folder)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true", help="check the index (pre-commit)")
    g.add_argument("--worktree", action="store_true", help="check every uncommitted change incl. untracked")
    g.add_argument("--unpushed", action="store_true", help="check commits not on any remote")
    g.add_argument("--range", nargs="+", metavar="REV", help="check commits selected by git log revs")
    g.add_argument("--pre-push", nargs="*", metavar="ARG", help="read pre-push refs from stdin")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("commit", help="execute a commit plan (JSON), one commit per task")
    p.add_argument("plan", help="path to the plan JSON file")
    p.add_argument("--dry-run", action="store_true", help="show what would be committed/excluded")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("install-hooks", help="install pre-commit and pre-push guard hooks")
    add_repo_args(p)
    p.add_argument("--force", action="store_true", help="install even where core.hooksPath is set")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("uninstall-hooks", help="remove gitcheck hooks (restores previous hooks)")
    add_repo_args(p)
    p.set_defaults(func=cmd_uninstall_hooks)

    p = sub.add_parser("install-skill", help="(re)install the /smart-commit Cursor skill")
    p.add_argument("--target", help=f"skill folder (default: {skill.default_target()})")
    p.set_defaults(func=cmd_install_skill)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GitError as exc:
        print(f"gitcheck: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
