---
name: smart-commit
description: Groups uncommitted changes across one or more git repos into one commit per task, commits each task with its own files and message, and keeps env files, secrets, logs, build output and anything that only works on this computer (localhost / LAN addresses, local defaults, machine setup, unused local run setup) out of commits. Use when the user asks to commit their work ("commit my changes", "commit kar do"), split changes into proper commits, or check that nothing that should not be pushed is being committed.
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
- [ ] 5. Show every commit in full, ask Yes / No / Other (loop until Yes or No)
- [ ] 6. Commit with --yes
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

**Local-only check: do this for every file before it goes into any commit.**
Everything committed here ends up on the server and on every teammate's machine. These rules are
strict on purpose. They apply to every project and every kind of file, and none of them depends on
a specific file name, project or tool.

**Rule 1: what counts as local-only.** A file is local-only if it points at this machine
*anywhere*: `localhost`, `127.0.0.1`, `0.0.0.0`, `10.0.2.2` and other emulator addresses, LAN or
private-network IPs (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`), `*.local` hostnames,
`host.docker.internal`, this computer's name, home or user folder paths (`C:\Users\<name>\...`,
`/Users/<name>/...`, drive paths), a specific phone or emulator ID, or ports that only a local dev
setup uses. It counts wherever it appears: code, config, **default or fallback values**,
**comments**, **examples**, **build or run instructions**, and scripts. A fallback to localhost
counts (`${API_URL:-http://localhost:3000}`, `?? 'http://localhost:3000'`, `default: ...`).
"An env var can override it" does not make it safe: the default still ships.
The only exceptions are tests, and docs that explicitly explain local setup.

Files that set up this computer or a device (firewall or port rules, port forwarding, tunnels,
hosts-file edits, admin-only setup), personal material (notes, run-command cheat sheets, scratch
files, debug dumps, local DB dumps, IDE settings) and changes that only switch the app to local
values are local-only too.

**Rule 2: warnings are real until proven otherwise.** Treat every guard warning about a local
signal (shown as `LOCAL` in `gitcheck guard`, "EXCLUDED as local-only" in the preview) as correct.
You may dismiss one only when you open the line and can show it is **detection logic** (code that
checks for or rejects localhost, such as validation) or **test data**, not a value the app or the
build uses. Then put that evidence in the plan under `allow_local` for that file (step 5), quoting
the line. "It is only a default", "it is only a comment" and "an env var overrides it" are *not*
evidence. If you cannot show it, the file stays excluded. `BLOCK` findings can never be dismissed
this way.

**Rule 3: check new run / deploy setup before committing it.** For every *new* container file,
compose file, proxy config, run or start script, deploy manifest or pipeline file, check all three:
- **It is wired into the real build or deploy**: an existing CI / pipeline file, the README or an
  existing deploy script refers to it. Search the repo for its name (`git -C <repo> grep -n <name>`).
  Another file that is also new and also unreferenced does not count.
- **It does not duplicate an existing deploy path**: there is not already a Dockerfile / compose
  file / pipeline / proxy config doing the same job.
- **It does not pull local-only material into the build**: no env files (`.env*`), no local build
  output (`build/`, `dist/`, `out/` from this computer), no local settings. `COPY . .` / `ADD .`
  needs a `.dockerignore` that keeps `.env*` out.
If any check fails, the file is local-only. The guard runs the same checks (unreferenced, duplicate,
copies local material), but do them yourself as well.

**Rule 4: never reword a message to get past the flag.** If a commit message you wrote (or the
preview) sounds local ("local", "localhost", "my machine", "from phones", "firewall", "open port",
"port forward", "emulator", "debug only", "temporary", "run locally"...), the message was telling
the truth about the files. Re-check those files, drop the local ones, and state the reason. Keep a
flagged commit only when its files are clearly server-side, and then write why in the plan's
`server_reason`. The commit step remembers which files were in a flagged commit, so a new message
alone does not clear the flag; it skips the commit unless `server_reason` is given.
Example of a commit that must never happen:
`Add Windows firewall scripts to reach the local API from phones`.

**Rule 5: when unsure, exclude and tell the user.** Keeping a file out by mistake is easy to
fix; pushing it is not. Already staged is **not** approval: judge staged files like any other.

**Rule 6: how local-only files are shown.** Every file with any local-only signal goes under
"Excluded as local-only" with a plain reason. Never present such a file as "will be committed
with warnings". If you want to keep it, you need Rule 2 evidence in `allow_local`, and you must
show that evidence to the user.

**Guard findings:**
- `BLOCK`: never put them in the plan. The commit step drops them anyway, and they cannot be
  overridden from the plan.
- `WARN` with a local signal (`LOCAL` in guard output, `local: true` in `--json`): excluded by the
  commit step unless `allow_local` has Rule 2 evidence for that file.
- Other `WARN` (Firebase client configs, hardcoded-credential patterns...): judge them.
- The guard cannot know everything. Your judgment still applies to every file it lets through.

### 5. Show every commit in full, then Yes / No / Other

Nothing is committed before the user has seen every commit and said Yes. This applies to every
run, including "just commit everything" requests. The only exception is when the user explicitly
said in this request not to ask (for example "bina puchay commit kar do").

**a. Write the plan** to a JSON file in the OS temp folder (`$env:TEMP` on Windows, `/tmp` on macOS/Linux), never inside a repo:

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

Two optional fields per commit, only for the cases in step 4:
- `"allow_local": {"<file>": "<evidence>"}`: Rule 2 evidence that a local-only *warning* in that
  file is detection logic or test data. Quote the line, for example
  `"line 12 is the check that rejects localhost URLs in user input; the app never connects to it"`.
  It never overrides a `BLOCK`.
- `"server_reason": "<why>"`: Rule 4, only for a commit flagged as local-only whose files are
  clearly server-side.

**b. Preview:** `gitcheck commit <plan.json> --dry-run`. It prints, per repo: every commit with a
number `[N]`, its full title and body, and every file with its status (new / modified / deleted /
renamed) and `+added -deleted` lines. It also lists files marked `EXCLUDED as local-only: <reason>`
under "Excluded as local-only", files the guard excludes for other reasons, local-only warnings
dismissed with your evidence, other warnings, and files left uncommitted. If it excludes something
unexpected, re-check the file with the step 4 rules. Do not reword or move files just to make the
exclusion go away.

**c. Show it to the user** in full, in a code block, unshortened. Below it add:
- one line per commit saying what that task is, in plain words;
- "Excluded as local-only (stays on this computer, never pushed)", listing **every** file with any
  local-only signal, whether the guard found it or you did, with a plain reason saying what it points
  at or does on this computer (`<file> -- <reason>`). Never list such a file as committed with warnings;
- "Kept despite a local-only warning", only if you used `allow_local`: the file, the line and your
  evidence, so the user can disagree;
- "Excluded by me" with a reason per other file you left out on purpose (step 4), since the
  preview only lists those under "Left uncommitted".

If the preview shows `flagged LOCAL-ONLY` on a commit, or your own summary sounds local, do not ask
Yes / No / Other yet. Apply Rule 4: re-check that commit's files, drop the local ones, and preview
again. Keep the commit only when its files are clearly server-side, with `server_reason` in the plan,
and say why in the commit's line.

**d. Ask** with the AskQuestion tool, in the user's language, with exactly these three options:
- `Yes - commit all of this`
- `No - cancel, commit nothing`
- `Other - I want to change something`

**e. Handle the answer:**
- **Yes**: go to step 6.
- **No**: delete the plan file, say nothing was committed, stop.
- **Other** (or anything the user typed): ask what to change with a second AskQuestion
  (`allow_multiple: true`), built from the current plan:
  - `Skip commit [N]: <title>`, one option per commit;
  - `Remove <file> from commit [N]`, one option per file (with more than ~25 files, offer
    `Remove files from commit [N]` per commit instead and ask per file for the chosen commit);
  - `Change the message of commit [N]`;
  - `Move a file to another commit`;
  - `Add a left-out file`, when there are uncommitted files that the guard did not block. A file
    excluded as local-only goes back in only if the user confirms, and a local-only *warning* still
    needs `allow_local` evidence (ask the user for it if you have none). A `BLOCK` cannot be added
    back from the plan.
  The user can also type the problem in their own words ("commit 2 ka title change karo",
  "config file nikal do", "ye dono ek hi commit mein daal do"); work out what they mean. If a choice
  needs more input (a new message, a target commit), ask for it or propose a concrete value.
  Removed files and skipped commits stay in the working tree, uncommitted.
- After any change: update the plan JSON, run the preview again, show the full new output, and ask
  Yes / No / Other again. Repeat until the user says Yes or No.

### 6. Commit

Only after a Yes for the exact plan that was last shown:

```
gitcheck commit <plan.json> --yes
```

Without `--yes`, a non-interactive run prints the plan and exits with code 2 without committing, so
never add `--yes` before the user has approved. (When a person runs the command in a terminal
without `--yes`, the CLI shows the same preview and asks `[y] yes  [n] no  [o] other` itself.)

The real run clears the index (working tree untouched), commits each task, and guards each commit
before and after. It drops blocked and local-only files automatically, skips flagged commits
without `server_reason`, and restores the repo to its starting state if anything fails.

### 7. Report

Per repo: commits created (hash + subject), files excluded as local-only and why, files the guard
excluded for other reasons, commits skipped as flagged local-only, local-only warnings kept with
evidence, files left uncommitted, other warnings, and repos where hooks could not be installed.

**Some local-only files are hidden automatically.** The commit step adds every *untracked* file
that is local by nature (machine-setup scripts, local-named files, the user's path rules) to
`<repo>/.git/info/exclude`, whether or not it was in the plan, so a later `git add .` cannot pick
it up. They are marked `[will be hidden on this computer]` / `[hidden on this computer]` under
"Excluded as local-only". Tell the user which files were hidden and that deleting the line in
`.git/info/exclude` shows a file again. Files excluded only because of one value (a localhost
default in a needed compose file or config) are *not* hidden: they need that value fixed, so say
what to change (for example, no localhost default: the real URL comes from the server's env or build arg).

For other files that stayed behind as local-only by your judgment, ask with AskQuestion (in the
user's language):
- `Hide these files on this computer` -> append `/<path>` lines to `<repo>/.git/info/exclude`
  (never committed, only this computer);
- `Treat files like this as local-only in this repo` -> propose a general rule for the *kind* of
  file (a path glob like `scripts/dev-*.ps1`, or `content: <regex>` for a line pattern), show it,
  then append it to `<repo>/.commitguard-local` (the user may commit that file to share it);
- `Treat files like this as local-only in every project` -> same rule, appended to
  `~/.gitcheck/local-only`;
- `Add them to .gitignore` -> only for files no one on the team should ever commit (logs, build
  output, env files, `local.properties`);
- `No, leave them as they are`.
Do not edit any of these files without a yes.

**Already committed by mistake:** run `gitcheck guard -C <repo> --tracked` when the user asks
whether something local was committed before, or when this run excluded a local-only file whose
older version may already be in the repo. For each finding explain what it does and propose the
fix: a local-only file -> `git rm --cached -- <path>` plus `.gitignore` (the file stays on disk);
a localhost value inside a needed file -> replace it (env var / build arg without a localhost
default), then commit that fix through this same workflow. Do nothing without approval.
If a secret file is already tracked, tell the user it is already in history and needs
`git rm --cached` plus rotating the secret.

Delete the temp plan file. Do not push; if the user asks to push, the pre-push hook re-checks the commits.

## Guard reference

- `gitcheck guard -C <repo> --worktree | --staged | --unpushed | --range <revs>` checks changed paths and added lines; `--tracked` audits every file already committed.
- BLOCK: `.env*` (except `*.example`/`*.sample`/`*.template`), `*.log`, keys/keystores (`*.pem`, `*.key`, `*.jks`, `key.properties`, service-account JSON), private keys/tokens in content, `node_modules/`, `.dart_tool/`, `__pycache__/`, root `build/` / `dist/`.
- BLOCK, local-only (only tests are skipped; docs only warn):
  - any local or private-network URL (`localhost`, `127.x`, `0.0.0.0`, `[::1]`, `10.x`, `172.16-31.x`, `192.168.x`, `*.local`, `host.docker.internal`) in code, config, compose files, Dockerfiles, proxy configs, CI files, scripts, `*.example` templates and no-extension files, **including default / fallback values and comments / build instructions**;
  - a connection setting assigned a bare localhost (`API_HOST=localhost:3000`, `host: 'localhost'`, `proxy: 127.0.0.1`);
  - LAN / emulator addresses (`192.168.x.x`, `172.16-31.x.x`, `10.0.2.2`, `10.0.3.2`) anywhere except docs;
  - this computer's identity: `C:\Users\<name>\...` / `/Users/<name>/` paths, its computer name, its LAN IPs (worked out at runtime); drive paths like `D:\Project\...` outside code;
  - in scripts, `.txt` notes and extension-less files: firewall / port rules, hosts-file edits, run-as-administrator setup, phone / emulator port forwarding, tunnels, runs on one specific device;
  - scripts named for machine setup (`*firewall*`, `allow-port*`, `open-port*`, `port-forward*`, `portproxy*`);
  - local-named files: `*.local`, `*.local.*`, compose override files, non-code files named like `run_local.ps1`, `local.properties`;
  - container builds (any Dockerfile / Containerfile) that `COPY`/`ADD` env files, local build output (`build/`, `dist/`, `out/`, ...), local settings, or the whole folder without a `.dockerignore` that keeps `.env*` out;
  - the user's own rules: `~/.gitcheck/local-only` (every repo) and `<repo>/.commitguard-local` (one repo), one path glob per line or `content: <regex>`.
- WARN, local-only (excluded unless `allow_local` evidence): a bare `localhost` mention that is not a setting (often detection logic), drive paths in code, local addresses / machine setup in docs, `tmp/`, `scratch/`, `uploads/`, `.idea/`, debug `.txt` output, `*.bak` / `*.tmp` / `*.orig`; **new** run / deploy setup that nothing in the project references, or that duplicates an existing deploy path.
- WARN, judge it: `ufw` / `iptables` / `firewall-cmd` (fine in server provisioning), `Set-ExecutionPolicy`, dev run commands in scripts, `google-services.json`, hardcoded credentials.
- A real false positive: add the path to `.commitguard-allow` in the repo root, or put `commitguard: allow` on the line. Only use this for the guard's own detection code, tests, or a case the user confirmed; never to get a local value through.
- Other commands: `gitcheck projects list|add|remove`, `gitcheck doctor`.
