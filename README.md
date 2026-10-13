# Gitcheck

Task-aware commits and safe merges across your git repos, with a guard that keeps files out of
commits when they must never be pushed.

- **`/smart-commit`**: your AI agent groups your uncommitted changes into one commit per task,
  shows you the full plan, and commits only after you say yes. Env files, secrets, logs, build
  output and anything that only works on your computer (localhost / LAN addresses, your folder
  paths, machine setup scripts) are left out.
- **`/smart-merge`**: previews merging your branch into `main` (every commit, file and expected
  conflict), asks for approval, then merges locally. Pushes only if you ask.
- **Git hooks**: `pre-commit` and `pre-push` hooks run the guard on every commit and push, whether
  it comes from an agent or from you in a terminal.

Works in **Cursor** and **Claude Code** (CLI or IDE extension). The AI part is done by the model
you use in that chat; Gitcheck itself needs no API key. It does not work in the Claude.ai website
or chat app, because those cannot run commands on your computer.

## Requirements

- Python 3.9 or newer. On Windows, tick **"Add python.exe to PATH"** in the installer.
- git
- No other packages.

## Install

```sh
git clone https://github.com/SyedAamir07/Gitcheck.git
cd Gitcheck
```

Then run the setup:

- Windows: double-click `setup.bat` (or run it from a terminal)
- macOS / Linux / Git Bash: `sh setup.sh`

Setup asks which folders hold your projects (a repo, or a parent folder with several repos), then:

1. saves them to `~/.gitcheck/config.json` (only on your computer, not in this repo);
2. installs the `/smart-commit` and `/smart-merge` skills for every client it finds:
   `~/.cursor/skills` for Cursor, `~/.claude/skills` for Claude Code;
3. installs the guard hooks in those projects (it asks first).

Restart Cursor / Claude Code afterwards so they pick up the skills. Check everything with:

```sh
python gitcheck.py doctor
```

Keep the cloned folder where it is: the skills and hooks point to it. If you move it, run
`python gitcheck.py setup` again.

## Use

In the Cursor or Claude Code chat, inside one of your projects:

- `/smart-commit` (or just "commit my changes")
- `/smart-merge` (or "merge this branch into main")

The agent always shows the full plan and asks Yes / No / Other before it commits or merges.
Nothing is pushed unless you ask.

## Commands

Run as `python gitcheck.py <command>` (on Windows `py -3 gitcheck.py <command>` also works).

| Command | What it does |
| --- | --- |
| `setup` | First-time setup: projects, skills, hooks |
| `doctor` | Checks Python, git, skills and hooks |
| `projects [list\|add\|remove] <folder>` | Manage your saved project folders |
| `status` | Uncommitted changes and guard findings for every repo |
| `guard -C <repo> --worktree` | Check uncommitted changes (also `--staged`, `--unpushed`, `--tracked`) |
| `commit <plan.json> --dry-run` | Preview a commit plan without committing |
| `merge-plan` | Preview merging the current branch into main |
| `install-hooks` / `uninstall-hooks` | Add or remove the guard hooks |
| `install-skill` | Reinstall the skills (run after updating Gitcheck) |
| `uninstall` | Remove the skills, the hooks from saved projects, and the config |

Add `--help` to any command for its options.

## What the guard blocks

- **Always blocked**: `.env*` (except `*.example` / `*.sample` / `*.template`), `*.log`, private
  keys and keystores, service-account JSON, private keys or tokens in file content,
  `node_modules/`, `__pycache__/`, root `build/` / `dist/`.
- **Local-only, kept out of commits**: localhost / LAN / emulator addresses (including defaults
  and comments), paths to your user folder, your computer's name, firewall / port-forwarding
  scripts, `*.local` files, and new run or deploy setup that nothing in the project uses.
- **Warnings**: `google-services.json`, `.idea/`, `*.bak`, `*.tmp` and similar.

### Your own rules

- `~/.gitcheck/local-only` (every repo) or `<repo>/.commitguard-local` (one repo): one path glob
  per line, or `content: <regex>`, for files or lines that are local-only for you.
- `<repo>/.commitguard-allow`: one path glob per line that the guard should skip (real false
  positives only).
- `commitguard: allow` on a line skips content checks for that line.

## Update

```sh
git pull
python gitcheck.py install-skill
python gitcheck.py doctor
```

## Tests

```sh
python -m pytest -q
```
