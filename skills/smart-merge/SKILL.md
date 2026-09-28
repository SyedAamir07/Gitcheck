---
name: smart-merge
description: Merges the current branch (or a named branch) into main safely. Shows every commit and file that will land on main, predicts conflicts, runs the guard on the incoming commits, asks Yes / No / Other, resolves conflicts on the feature branch with the user's approval, then merges locally and pushes only if asked. Use when the user asks to merge their work into main ("main mein merge kar do", "merge this branch", "branch ko main mein daal do") or types /smart-merge.
---

# Smart Merge

The Gitcheck CLI does the deterministic work (preview, guard, conflict prediction, the merge itself
with rollback on failure). Your job is the judgment: fixing what blocks the merge, resolving
conflicts sensibly, choosing a strategy and message, and getting the user's approval.

Run the CLI from the workspace root:

```
"{{PYTHON}}" "{{GITCHECK}}" <command>
```

In PowerShell prefix it with `&`: `& "{{PYTHON}}" "{{GITCHECK}}" <command>`. Below, `gitcheck <command>` means this full invocation.

Never force-push. Never use `--no-verify`. Never `reset --hard`, rebase, or rewrite commits that are
already on the target or on a remote. Never delete a branch unless the user chose that option.
Talk to the user in their language.

## Workflow

```
- [ ] 1. Preview the merge
- [ ] 2. Fix what blocks it (uncommitted work, guard, diverged main)
- [ ] 3. Resolve conflicts on the feature branch (only if conflicts are expected)
- [ ] 4. Show the merge in full, ask Yes / No / Other (loop until Yes or No)
- [ ] 5. Merge with --yes
- [ ] 6. Report
```

### 1. Preview

```
gitcheck merge-plan --fetch --json
```

Defaults: source = the current branch, target = origin's default branch (else `main` / `master`).
- User named a branch or target ("feature/login ko develop mein"): add `--source <branch>` / `--into <branch>`.
- User named projects: pass their folders as arguments. "All my projects": add `--configured`.

Each entry has `source`, `target`, `base_rev` (what the source is compared with; `origin/main` when
the local main is only behind), `commits`, `files` (+/- lines), `conflicts`, `can_fast_forward`,
`findings` (guard), `notes` and `merge_problems` (why it cannot be merged yet).
An empty list means there is nothing to merge (the user is on main, or the branch is already merged): say so and stop.

### 2. Fix blockers

Handle each `merge_problems` entry; never work around the checks.
- **Uncommitted changes in tracked files**: ask whether to commit them first. If yes, follow the
  `/smart-commit` skill (read it), then preview again. Do not stash unless the user asks.
- **Local target diverged from origin**: explain it (both sides have commits the other does not)
  and ask how to proceed. Do not reset or rebase the target on your own.
- **Guard blocks files in the incoming commits** (`.env`, keys, logs, localhost URLs): show the
  file and commit. If it is a real problem, the commits must be fixed on the feature branch before
  merging; propose how and do it only after approval. If it is a false positive, offer to add the
  path to `.commitguard-allow` (see the guard reference in `/smart-commit`).
- **A merge/rebase is in progress**: tell the user; do not continue.

### 3. Conflicts (only when `conflicts` is not empty)

Conflicts are resolved on the feature branch, never on main. Ask with AskQuestion:
`Resolve the conflicts on <source> (recommended)` / `Cancel`.

To resolve:
1. `git -C <repo> checkout <source>` then `git -C <repo> merge <base_rev>` (it stops with conflicts).
2. For every conflicted file: read it, and see what each side intended
   (`git -C <repo> log --oneline <base_rev>..<source> -- <file>` and the same for `<source>..<base_rev>`).
   Keep both intents where possible. If the two sides change the same logic in incompatible ways,
   ask the user with AskQuestion, showing both versions briefly: `Keep <source> version` /
   `Keep <target> version` / `Combine them` (and let them type details).
3. Make sure no conflict markers are left (`<<<<<<<`, `=======`, `>>>>>>>`). If the project has a
   quick test or build command, run it and report the result.
4. Show a summary per file (what was kept from each side) plus `git -C <repo> diff --cached --stat`,
   and ask `Yes - save the resolution` / `No - undo it` / `Other`.
   - Yes: `git -C <repo> add <files>` then `git -C <repo> commit --no-edit`.
   - No: `git -C <repo> merge --abort`, then check out the branch the user started on.
5. Preview again (step 1).

### 4. Show the merge in full, then Yes / No / Other

Pick the strategy and message first:
- `merge` (merge commit, default): keeps every commit. Use when the target history already has merge commits.
- `squash`: one commit on the target. Suggest it when the branch is full of small WIP/fix commits or
  the target history is linear. Write a good message: a Conventional Commit summary of the whole
  branch, plus a short body.
- `ff`: only when `can_fast_forward` is true and the user wants no merge commit.

Then show the preview as text, unshortened, in a code block:

```
gitcheck merge-plan --source <source> --into <target> --strategy <strategy>
```

It lists the source and target, strategy, every commit (hash, subject, author, date), every file
with `+added -deleted`, conflicts, guard findings, the message, and what happens after the merge.
Below it, add two or three plain sentences on what this branch does, based on the commits.

Ask with AskQuestion, with exactly these options:
- `Yes - merge into <target> (local only)`
- `Yes - merge and push <target> to origin`
- `No - cancel, merge nothing`
- `Other - I want to change something`

Handle the answer:
- **Yes**: go to step 5, with `--push` only for the second option.
- **No**: say nothing was merged, stop.
- **Other** (or anything the user typed): ask what to change with a second AskQuestion
  (`allow_multiple: true`): `Use squash (one commit)`, `Use a merge commit`,
  `Use fast-forward` (only if possible), `Change the merge message`, `Merge into a different branch`,
  `Delete <source> after merging`. The user can also type freely ("message mein ticket number
  daal do", "develop mein merge karo"); work out what they mean and ask for any missing value.
  Apply it, show the full preview again, and ask Yes / No / Other again. Repeat until Yes or No.

### 5. Merge

Only after a Yes for the exact preview that was last shown:

```
gitcheck merge -C <repo> --source <source> --into <target> --strategy <strategy> [--message "<msg>"] [--delete-branch] [--push] --yes
```

`--message` uses `\n` for new lines. Without `--yes`, a non-interactive run prints the preview and
exits with code 2 without merging, so never add `--yes` before the user approved. (A person running
`gitcheck merge` in a terminal gets the same preview and a `[y] yes  [n] no  [o] other` prompt.)

The command checks everything again, fast-forwards the local target to origin if it is only behind,
merges, deletes the branch / pushes if asked, and returns to the branch the user started on. If
anything fails (including unexpected conflicts) it aborts the merge and leaves the target as it was.

### 6. Report

Per repo: the new commit(s) on the target, whether it was pushed, whether the branch was deleted,
and which branch is checked out now. If it was not pushed, give the push command
(`git push origin <target>`; the pre-push hook re-checks the commits). If the push was rejected
(protected branch, or origin has new commits), explain it: for a protected branch suggest opening a
pull request instead; never force-push.
