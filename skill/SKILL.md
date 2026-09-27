---
name: smart-commit
description: Groups uncommitted changes across one or more git repos into one commit per task, commits each task with its own files and message, and keeps env files, secrets, logs, localhost URLs and build output out of commits. Use when the user asks to commit their work ("commit my changes", "commit kar do"), split changes into proper commits, or check that nothing that should not be pushed is being committed.
---

# Smart Commit

The Gitcheck CLI does the deterministic work (finding repos, guard checks, executing commits).
Your job is the judgment: deciding which changed files belong to which task, and writing good messages.

Run the CLI from the workspace root:

```
"{{PYTHON}}" "{{GITCHECK}}" <command>
```

In PowerShell prefix it with `&`: `& "{{PYTHON}}" "{{GITCHECK}}" <command>`. Below, `gitcheck <command>` means this full invocation.

Never push. Never use `--no-verify`. Never edit, delete or `git checkout` the user's files.

## Workflow

```
- [ ] 1. Snapshot the repos
- [ ] 2. Make sure guard hooks are installed
- [ ] 3. Understand every change
- [ ] 4. Group changes into tasks
- [ ] 5. Show the plan and get approval
- [ ] 6. Dry run, then commit
- [ ] 7. Report
```

### 1. Snapshot

```
gitcheck status --json
```

Repo selection: the repo containing the current folder, or the repos inside it (two levels deep).
If none are found it falls back to the projects the user saved during setup.
- User named specific projects: pass their folders as arguments.
- User said "all my projects": add `--configured`.

Each repo lists `branch`, `changes` (status code, +/- lines, `note`), `findings` (guard results)
and `hooks_installed`. If `operation` is set (merge/rebase in progress), skip that repo and tell the user.
If the output is empty, there is nothing to commit; say so and stop.

### 2. Hooks

For every repo with `hooks_installed: false`, run `gitcheck install-hooks <repo> [<repo> ...]`.
If it reports `skipped: core.hooksPath is set`, mention it in the final report; do not force it.

### 3. Understand the changes

Per repo:
- `git -C <repo> diff HEAD --stat`, then `git -C <repo> diff HEAD -- <files>` for tracked files; read untracked (`??`) files directly.
- `git -C <repo> log -15 --oneline` to learn the repo's commit message style and language.
- Relationships matter: imports between new and changed files, a route + its service + its test, a migration + the model it changes.
- If a task is still unclear, look at the recent agent chats for this workspace; they usually say what the user was working on. Use any task names the user gave when invoking this skill.

For large diffs, read the stat first and only open files whose purpose is unclear.

### 4. Group into tasks

One commit = one logical change (feature, bug fix, refactor, config change, dependency bump).

- Group by intent, not by folder.
- Generated files follow their cause: lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `pubspec.lock`, `poetry.lock`, `Cargo.lock`, `go.sum`) go with the manifest change that caused them; Flutter `generated_plugin_registrant.*` / `generated_plugins.cmake` / `GeneratedPluginRegistrant.swift` go with the dependency change. With no such change in the batch: one `chore` commit.
- Files whose `note` says "no content diff" only changed line endings or mode: leave them out and list them under "Left uncommitted".
- A file mixing two tasks goes with the dominant task; say so in the plan. Do not split hunks.
- Order: foundations first (dependencies, config, migrations), then features that use them.

Messages: follow the repo's existing style. If there is none, use Conventional Commits:
`type(scope): imperative summary` (max ~72 chars), types `feat fix refactor perf chore docs test build ci style`.
Add a body only when the "why" is not obvious from the summary.

**Exclusions — decide per file:**
- Guard `BLOCK` findings: never put them in the plan (the commit step would drop them anyway).
- Guard `WARN` findings: judge them. `*.local.*` configs, `tmp/`, `scratch/`, debug `.txt` output, localhost in non-code files: usually exclude. Firebase client configs (`google-services.json`, `firebase_options.dart`): usually fine for mobile apps.
- Also exclude on your own: clearly personal or experimental files (scratch notes, debug dumps, one-off scripts), IDE files, and code changes that only switch URLs/flags to local development values.

### 5. Plan and approval

Show the plan per repo before committing anything:

```
api (branch: main)
  1. feat(auth): add password reset endpoint
     src/routes/auth.ts, src/services/reset.ts, test/reset.test.ts
  2. chore(deps): bump express to 4.19
     package.json, package-lock.json
  Excluded (will not be committed):
     server.log -- log file
     config/app.local.json -- local-only config
  Left uncommitted: (none)
```

Then use the AskQuestion tool: "Commit as planned" / "Change the plan" / "Cancel". Apply requested
changes and ask again. Skip approval only if the user explicitly said to commit without asking.

### 6. Dry run, then commit

Write the plan to a JSON file in the OS temp folder (`$env:TEMP` on Windows, `/tmp` on macOS/Linux), never inside a repo:

```json
{
  "repos": [
    {
      "path": "<absolute repo path from status>",
      "commits": [
        {"message": "feat(auth): add password reset endpoint", "files": ["src/routes/auth.ts", "src/services/reset.ts", "test/reset.test.ts"]},
        {"message": "chore(deps): bump express to 4.19", "files": ["package.json", "package-lock.json"]}
      ]
    }
  ]
}
```

File paths are relative to the repo with forward slashes, exactly as in `status`. For renames include
the old path too. Multi-line messages use `\n` (summary, blank line, body).

```
gitcheck commit <plan.json> --dry-run
gitcheck commit <plan.json>
```

If the dry run excludes something unexpected, fix the plan first. The real run clears the index
(working tree untouched), commits each task, guards each commit before and after, drops blocked
files automatically, and restores the repo to its starting state if anything fails.

### 7. Report

Per repo: commits created (hash + subject), files the guard excluded and why, files left
uncommitted, warnings, and repos where hooks could not be installed. For excluded files that should
never be committed, offer to add them to `.gitignore`. If a secret file is already tracked
(`git ls-files` lists it), tell the user it is already in history and needs `git rm --cached` plus
rotating the secret; do not do this without approval.

Delete the temp plan file. Do not push; if the user asks to push, the pre-push hook re-checks the commits.

## Guard reference

- `gitcheck guard -C <repo> --worktree | --staged | --unpushed | --range <revs>` checks changed paths and added lines.
- BLOCK: `.env*` (except `*.example`/`*.sample`/`*.template`), `*.log`, keys/keystores (`*.pem`, `*.key`, `*.jks`, `key.properties`, service-account JSON), private keys/tokens in content, `http(s)://` URLs to localhost / `127.0.0.1` / `10.0.2.2` / `192.168.x.x` in non-test source code, `node_modules/`, `.dart_tool/`, `__pycache__/`, root `build/` / `dist/`.
- WARN: `*.local.*`, `google-services.json`, `tmp/`, `uploads/`, `.idea/`, debug `.txt` output, hardcoded credentials, localhost in non-code files.
- False positive the user wants to keep: add the path to `.commitguard-allow` in the repo root, or put `commitguard: allow` on the line.
- Other commands: `gitcheck projects list|add|remove`, `gitcheck doctor`.
