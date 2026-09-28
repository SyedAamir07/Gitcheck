import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitcheck import committer, guard, hooks, status  # noqa: E402
from gitcheck.gitops import git  # noqa: E402


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="gitcheck-test-")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "test@example.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "commit.gpgsign", "false", cwd=self.repo)
        write(self.repo, "README.md", "hello\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def log(self):
        return git("log", "--format=%s", cwd=self.repo).splitlines()

    def files_in(self, rev):
        return sorted(git("diff-tree", "--no-commit-id", "--name-only", "-r", rev, cwd=self.repo).split())

    def plan(self, commits):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"repos": [{"path": self.repo, "commits": commits}]}, fh)
        self.addCleanup(os.remove, path)
        return path


class GuardRules(unittest.TestCase):
    def levels(self, path):
        return [f.level for f in guard.check_path(path)]

    def test_blocked_paths(self):
        for p in [".env", "backend/.env.production", "backend_run.log", "upload-keystore.jks",
                  "android/key.properties", "x-firebase-adminsdk-abc.json", "node_modules/a/b.js",
                  "build/app.js", "lib/.DS_Store"]:
            self.assertIn(guard.BLOCK, self.levels(p), p)

    def test_templates_and_normal_files_pass(self):
        for p in [".env.example", ".env.production.example", "lib/main.dart", "src/app.ts",
                  "lib/src/build/widget.dart"]:
            self.assertEqual(self.levels(p), [], p)

    def test_warned_paths(self):
        for p in ["deploy/livekit.local.yaml", "android/app/google-services.json", "tmp/x.txt",
                  "flutter_build_err.txt"]:
            self.assertEqual(self.levels(p), [guard.WARN], p)

    def test_content(self):
        def levels(path, line):
            return [f.level for f in guard.check_content(path, guard.Change(added=[(1, line, "")]))]

        self.assertEqual(levels("lib/api.dart", "const base = 'http://localhost:3000/api';"), [guard.BLOCK])
        self.assertEqual(levels("lib/api.dart", "const base = 'http://10.0.2.2:3000';"), [guard.BLOCK])
        self.assertEqual(levels("test/api_test.dart", "const base = 'http://localhost:3000';"), [])
        self.assertEqual(levels("docker-compose.yml", "URL: http://localhost:3000"), [guard.WARN])
        self.assertEqual(levels("lib/api.dart", "const base = 'https://api.example.com';"), [])
        self.assertEqual(levels("lib/api.dart", "x = 'http://localhost' // commitguard: allow"), [])
        self.assertEqual(levels("a.txt", "-----BEGIN RSA PRIVATE KEY-----"), [guard.BLOCK])
        self.assertEqual(levels("src/cfg.ts", "password = 'hunter2hunter2'"), [guard.WARN])
        self.assertEqual(levels("src/cfg.ts", "password = process.env.DB_PASSWORD"), [])


class GuardOnRepo(RepoTestCase):
    def test_worktree_and_staged(self):
        write(self.repo, ".env", "SECRET=1\n")
        write(self.repo, "lib/api.dart", "final url = 'http://localhost:8080';\n")
        write(self.repo, "lib/ok.dart", "void main() {}\n")
        found = guard.blocked_paths(guard.run_checks(self.repo, guard.collect_worktree(self.repo)))
        self.assertEqual(sorted(found), [".env", "lib/api.dart"])

        git("add", "lib/ok.dart", cwd=self.repo)
        self.assertEqual(guard.run_checks(self.repo, guard.collect_staged(self.repo)), [])

    def test_allowlist(self):
        write(self.repo, ".commitguard-allow", "deploy/\n*.log\n")
        write(self.repo, "deploy/x.local.yaml", "a: 1\n")
        write(self.repo, "run.log", "x\n")
        paths = {f.path for f in guard.run_checks(self.repo, guard.collect_worktree(self.repo))}
        self.assertEqual(paths, set())

    def test_unpushed_range(self):
        write(self.repo, "app.log", "x\n")
        git("add", "-f", "app.log", cwd=self.repo)
        git("commit", "-q", "-m", "oops", cwd=self.repo)
        found = guard.run_checks(self.repo, guard.collect_revs(self.repo, [["HEAD~1..HEAD"]]))
        self.assertEqual([(f.path, f.level) for f in found], [("app.log", guard.BLOCK)])
        self.assertEqual(len(found[0].commits), 1)


class Committer(RepoTestCase):
    def test_commits_per_task_and_excludes_blocked(self):
        write(self.repo, "src/auth.ts", "export const login = 1;\n")
        write(self.repo, "src/auth.test.ts", "test('x', () => {});\n")
        write(self.repo, "src/chart.ts", "export const chart = 1;\n")
        write(self.repo, "server.log", "boom\n")
        write(self.repo, "README.md", "hello world\n")

        plan = self.plan([
            {"message": "feat(auth): add login", "files": ["src/auth.ts", "src/auth.test.ts", "server.log"]},
            {"message": "feat(chart): add chart", "files": ["src/chart.ts"]},
        ])
        [r] = committer.run_plan(plan)

        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.log(), ["feat(chart): add chart", "feat(auth): add login", "init"])
        self.assertEqual(self.files_in("HEAD~1"), ["src/auth.test.ts", "src/auth.ts"])
        self.assertEqual(self.files_in("HEAD"), ["src/chart.ts"])
        self.assertIn("server.log", r.excluded)
        self.assertEqual(r.left_uncommitted, ["README.md"])
        self.assertTrue(os.path.exists(os.path.join(self.repo, "server.log")))

    def test_localhost_file_excluded_and_commit_skipped(self):
        write(self.repo, "lib/config.dart", "const api = 'http://localhost:3000';\n")
        plan = self.plan([{"message": "chore: point to local api", "files": ["lib/config.dart"]}])
        [r] = committer.run_plan(plan)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.log(), ["init"])
        self.assertIn("lib/config.dart", r.excluded)
        self.assertEqual(len(r.skipped), 1)

    def test_dry_run_changes_nothing(self):
        write(self.repo, "a.py", "x = 1\n")
        write(self.repo, ".env", "A=1\n")
        git("add", ".env", "-f", cwd=self.repo)
        plan = self.plan([{"message": "feat: a", "files": ["a.py", ".env"]}])
        [r] = committer.run_plan(plan, dry_run=True)
        self.assertEqual(self.log(), ["init"])
        self.assertIn(".env", r.excluded)
        self.assertEqual(r.commits[0].files, ["a.py"])
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.repo).split(), [".env"])

    def test_failed_commit_restores_state(self):
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        write(self.repo, "a.py", "x = 1\n")
        write(self.repo, "b.py", "y = 1\n")
        git("add", "b.py", cwd=self.repo)
        plan = self.plan([{"message": "feat: a", "files": ["a.py"]}])
        [r] = committer.run_plan(plan)
        self.assertFalse(r.ok)
        self.assertEqual(self.log(), ["init"])
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=self.repo).split(), ["b.py"])

    def test_deleted_and_renamed_files(self):
        write(self.repo, "old.py", "x = 1\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "add old", cwd=self.repo)
        git("mv", "old.py", "new.py", cwd=self.repo)
        os.remove(os.path.join(self.repo, "README.md"))
        plan = self.plan([
            {"message": "refactor: rename old to new", "files": ["new.py", "old.py"]},
            {"message": "docs: drop readme", "files": ["README.md"]},
        ])
        [r] = committer.run_plan(plan)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.left_uncommitted, [])
        self.assertEqual(git("status", "--porcelain", cwd=self.repo).strip(), "")


class Review(RepoTestCase):
    def setUp(self):
        super().setUp()
        write(self.repo, "src/auth.ts", "export const login = 1;\n")
        write(self.repo, "src/auth.test.ts", "test('x', () => {});\n")
        write(self.repo, "src/chart.ts", "export const chart = 1;\n")
        write(self.repo, "server.log", "boom\n")
        self.plan_path = self.plan([
            {"message": "feat(auth): add login\n\nWhy it matters", "files": ["src/auth.ts", "src/auth.test.ts", "server.log"]},
            {"message": "feat(chart): add chart", "files": ["src/chart.ts"]},
        ])

    def run_review(self, answers):
        from gitcheck import review

        answers = iter(answers)
        shown = []
        decision, plan = review.review(
            committer.load_plan(self.plan_path), ask=lambda _: next(answers), out=shown.append
        )
        return decision, plan, "\n".join(shown)

    def test_preview_shows_every_commit_file_and_exclusion(self):
        _, _, shown = self.run_review(["n"])
        self.assertIn("[1] feat(auth): add login", shown)
        self.assertIn("| Why it matters", shown)
        self.assertIn("[2] feat(chart): add chart", shown)
        self.assertIn("src/auth.ts  (new, +1 -0)", shown)
        self.assertIn("server.log  (EXCLUDED by guard", shown)
        self.assertIn("Total: 2 commit(s), 3 file(s)", shown)

    def test_yes_and_no(self):
        from gitcheck import review

        self.assertEqual(self.run_review(["yes"])[0], review.APPROVED)
        self.assertEqual(self.run_review(["no"])[0], review.CANCELLED)
        self.assertEqual(self.log(), ["init"])

    def test_other_remove_file_skip_commit_and_edit_message(self):
        from gitcheck import review

        decision, plan, _ = self.run_review([
            "o", "f", "1", "2",
            "o", "s", "2",
            "o", "m", "1", "feat(auth): add login flow", "-",
            "y",
        ])
        self.assertEqual(decision, review.APPROVED)
        [(_, commits)] = plan
        self.assertEqual([c.message for c in commits], ["feat(auth): add login flow"])
        self.assertEqual(commits[0].files, ["src/auth.ts", "server.log"])

    def test_other_move_file_and_drop_empty_commit(self):
        _, plan, shown = self.run_review(["o", "v", "2", "1", "1", "y"])
        [(_, commits)] = plan
        self.assertEqual(len(commits), 1)
        self.assertIn("src/chart.ts", commits[0].files)
        self.assertIn("has no files left, removed it", shown)

    def test_cli_refuses_without_approval_when_not_interactive(self):
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitcheck.py"),
             "commit", self.plan_path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("[1] feat(auth): add login", r.stdout)
        self.assertIn("approval needed", r.stderr)
        self.assertEqual(self.log(), ["init"])


@unittest.skipUnless(shutil.which("git"), "git required")
class Hooks(RepoTestCase):
    def commit(self):
        return subprocess.run(["git", "commit", "-m", "x"], cwd=self.repo, capture_output=True, text=True)

    def test_hook_blocks_bad_commit_and_allows_good_one(self):
        hooks.install(self.repo)
        self.assertTrue(hooks.hooks_installed(self.repo))

        write(self.repo, "debug.log", "x\n")
        git("add", "-f", "debug.log", cwd=self.repo)
        r = self.commit()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("debug.log", r.stdout + r.stderr)

        git("reset", "-q", cwd=self.repo)
        write(self.repo, "ok.py", "x = 1\n")
        git("add", "ok.py", cwd=self.repo)
        r = self.commit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_existing_hook_is_kept_and_restored(self):
        existing = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(existing, "w", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        hooks.install(self.repo)
        self.assertTrue(os.path.exists(existing + ".local"))
        hooks.uninstall(self.repo)
        self.assertFalse(hooks.hooks_installed(self.repo))
        self.assertFalse(os.path.exists(existing + ".local"))
        with open(existing) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\nexit 0\n")


class Status(RepoTestCase):
    def test_status_lists_changes_and_findings(self):
        write(self.repo, "a.py", "x = 1\ny = 2\n")
        write(self.repo, "app.log", "x\n")
        st = status.repo_status(self.repo)
        self.assertIsNone(st.error)
        self.assertEqual({c.path for c in st.changes}, {"a.py", "app.log"})
        self.assertEqual(next(c for c in st.changes if c.path == "a.py").added, 2)
        self.assertEqual([f.path for f in st.findings if f.level == guard.BLOCK], ["app.log"])


def make_repo(path):
    os.makedirs(path, exist_ok=True)
    git("init", "-q", cwd=path)
    return path


class SetupAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitcheck-setup-")
        self.skill_dir = os.path.join(self.tmp, "skills", "smart-commit")
        self.old_env = os.environ.get("GITCHECK_CONFIG")
        os.environ["GITCHECK_CONFIG"] = os.path.join(self.tmp, "cfg", "config.json")

        self.work = os.path.join(self.tmp, "work")
        self.a = make_repo(os.path.join(self.work, "app"))
        self.b = make_repo(os.path.join(self.work, "clients", "web"))
        os.makedirs(os.path.join(self.work, "node_modules", "pkg", ".git"))
        os.makedirs(os.path.join(self.work, "notes"))

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("GITCHECK_CONFIG", None)
        else:
            os.environ["GITCHECK_CONFIG"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovery_depth_and_skips(self):
        from gitcheck.gitops import discover_repos, find_repos

        self.assertEqual(discover_repos(self.work, 1), [self.a])
        self.assertEqual(sorted(discover_repos(self.work, 2)), sorted([self.a, self.b]))
        self.assertEqual(find_repos(os.path.join(self.a)), [os.path.normpath(self.a)])
        os.makedirs(os.path.join(self.a, "lib", "src"))
        self.assertEqual(
            [os.path.normcase(p) for p in find_repos(os.path.join(self.a, "lib", "src"))],
            [os.path.normcase(os.path.normpath(self.a))],
        )

    def test_setup_saves_projects_installs_skill_and_hooks(self):
        from gitcheck import config, installer, skill

        rc = installer.setup([self.work], install_hooks=True, assume_yes=True, skill_target=self.skill_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(config.projects()), sorted([self.a, self.b]))
        self.assertEqual(skill.state(self.skill_dir), "ok")
        with open(os.path.join(self.skill_dir, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("{{", text)
        self.assertIn(hooks.ENTRY_SCRIPT.replace("\\", "/"), text)
        self.assertTrue(hooks.hooks_current(self.a) and hooks.hooks_current(self.b))

        installer.setup([self.a], install_hooks=False, assume_yes=True, skill_target=self.skill_dir)
        self.assertEqual(len(config.projects()), 2)

        self.assertEqual(installer.doctor(self.skill_dir), 0)
        shutil.rmtree(self.b)
        self.assertEqual(installer.doctor(self.skill_dir), 1)

        config.remove_projects([self.b])
        self.assertEqual(config.projects(), [self.a])

        installer.uninstall(keep_config=False, skill_target=self.skill_dir)
        self.assertEqual(skill.state(self.skill_dir), "missing")
        self.assertFalse(hooks.hooks_installed(self.a))
        self.assertEqual(config.projects(), [])

    def test_hooks_path_repo_is_skipped(self):
        git("config", "core.hooksPath", ".husky", cwd=self.a)
        notes = hooks.install(self.a)
        self.assertTrue(notes[0].startswith("skipped"))
        self.assertFalse(os.path.exists(os.path.join(self.a, ".husky", "pre-commit")))
        hooks.install(self.a, force=True)
        self.assertTrue(os.path.exists(os.path.join(self.a, ".husky", "pre-commit")))


if __name__ == "__main__":
    unittest.main()
