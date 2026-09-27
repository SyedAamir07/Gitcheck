"""First-run setup, health check and uninstall."""
from __future__ import annotations

import os
import shutil
import sys

from . import config, hooks, skill
from .gitops import find_repos, git, is_repo

MIN_PYTHON = (3, 9)


def _ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes", "h", "haan", "ha")


def _prompt_folders() -> list[str]:
    print("Which folders are your projects?")
    print("  Enter a git repo folder, or a parent folder that contains several repos.")
    print("  One folder per line. Press Enter on an empty line when you are done.")
    repos: list[str] = []
    while True:
        try:
            raw = input("  folder: ").strip()
        except EOFError:
            break
        if not raw:
            break
        found = find_repos(raw)
        if not found:
            print("    no git repos found there (checked the folder and two levels below it)")
            continue
        for r in found:
            print(f"    + {r}")
        repos.extend(found)
    return repos


def _check_requirements() -> bool:
    ok = True
    if sys.version_info < MIN_PYTHON:
        print(f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, found {sys.version.split()[0]}")
        ok = False
    if not shutil.which("git"):
        print("ERROR: git is not installed or not on PATH (https://git-scm.com/downloads)")
        ok = False
    return ok


def _install_hooks(repos: list[str], force: bool) -> None:
    for r in repos:
        print(f"  {r}")
        for note in hooks.install(r, force=force):
            print(f"    {note}")


def setup(
    folders: list[str],
    install_hooks: bool | None,
    assume_yes: bool,
    force_hooks: bool = False,
    skill_target: str | None = None,
) -> int:
    print("Gitcheck setup\n")
    if not _check_requirements():
        return 1

    interactive = sys.stdin.isatty() and not assume_yes
    repos: list[str] = []
    for f in folders:
        found = find_repos(f)
        if not found:
            print(f"WARNING: no git repos found in {f}")
        repos.extend(found)
    if not folders and interactive:
        repos = _prompt_folders()

    unique: dict[str, str] = {}
    for r in repos:
        unique.setdefault(os.path.normcase(r), r)
    repos = list(unique.values())

    added = config.add_projects(repos)
    print(f"\nProjects saved to {config.config_path()} ({len(added)} new, {len(config.projects())} total)")
    for p in config.projects():
        print(f"  {p}{'' if is_repo(p) else '   (missing - remove with: projects remove)'}")

    dest = skill.install(skill_target)
    print(f"Cursor skill installed: {dest}")

    targets = [p for p in config.projects() if is_repo(p)]
    if targets:
        if install_hooks is None:
            install_hooks = _ask_yes_no(
                f"\nInstall guard hooks (pre-commit + pre-push) in {len(targets)} project(s)?"
            ) if interactive else True
        if install_hooks:
            print("\nInstalling hooks:")
            _install_hooks(targets, force_hooks)
    else:
        print("\nNo projects added yet. Add them later with:  python gitcheck.py projects add <folder>")

    print(
        "\nDone. How to use it:\n"
        "  1. Restart Cursor (or reload the window) so it picks up the new skill.\n"
        "  2. Open a project in Cursor, finish your work, then type in the chat:  /smart-commit\n"
        "  3. Check everything any time with:  python gitcheck.py doctor"
    )
    return 0


def add(folders: list[str], install_hooks: bool, force_hooks: bool = False) -> int:
    repos = [r for f in folders for r in find_repos(f)]
    if not repos:
        print("No git repos found in the given folder(s).")
        return 1
    for r in config.add_projects(repos):
        print(f"added: {r}")
    if install_hooks:
        print("Installing hooks:")
        _install_hooks(repos, force_hooks)
    return 0


def doctor(skill_target: str | None = None) -> int:
    problems = 0

    def line(ok: bool, text: str, fix: str = "") -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {text}" + (f"\n        fix: {fix}" if fix and not ok else ""))

    print("Gitcheck doctor")
    line(sys.version_info >= MIN_PYTHON, f"Python {sys.version.split()[0]} ({sys.executable})")
    has_git = bool(shutil.which("git"))
    line(has_git, f"git: {git('--version').strip() if has_git else 'not found'}", "install git")

    st = skill.state(skill_target)
    line(
        st == "ok",
        f"Cursor skill /{skill.SKILL_NAME}: {st} ({skill_target or skill.default_target()})",
        "python gitcheck.py install-skill",
    )

    projects = config.projects()
    print(f"\n  Projects ({config.config_path()}):")
    if not projects:
        print("    none - add with: python gitcheck.py projects add <folder>")
    for p in projects:
        if not is_repo(p):
            line(False, f"{p}: folder missing or not a git repo", f'python gitcheck.py projects remove "{p}"')
        elif hooks.custom_hooks_path(p):
            line(True, f"{p}: uses core.hooksPath ({hooks.custom_hooks_path(p)}), gitcheck hooks not managed")
        else:
            line(hooks.hooks_current(p), f"{p}: hooks", f'python gitcheck.py install-hooks "{p}"')

    print("\nAll good." if not problems else f"\n{problems} problem(s) found.")
    return 0 if not problems else 1


def uninstall(keep_config: bool, skill_target: str | None = None) -> int:
    for p in config.projects():
        if is_repo(p):
            print(p)
            for note in hooks.uninstall(p):
                print(f"  {note}")
    print(f"skill removed: {skill.uninstall(skill_target)}")
    if not keep_config and os.path.exists(config.config_path()):
        os.remove(config.config_path())
        print(f"config removed: {config.config_path()}")
    return 0
