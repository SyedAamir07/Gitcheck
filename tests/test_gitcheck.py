import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitcheck import committer, flags, guard, hooks, status  # noqa: E402
from gitcheck.gitops import git  # noqa: E402

_FLAGS_DIR = tempfile.mkdtemp(prefix="gitcheck-flags-")


def setUpModule():
    os.environ[flags.STORE_ENV] = os.path.join(_FLAGS_DIR, "flags.json")


def tearDownModule():
    os.environ.pop(flags.STORE_ENV, None)
    shutil.rmtree(_FLAGS_DIR, ignore_errors=True)


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        store = flags.store_path()
        if os.path.exists(store):
            os.remove(store)
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
                  "build/app.js", "lib/.DS_Store", "deploy/livekit.local.yaml", "docker-compose.override.yml",
                  "scripts/run_local.ps1", "android/local.properties", "scripts/allow-port-3000.bat",
                  "tools/open-port.sh", "scripts/firewall.ps1"]:
            self.assertIn(guard.BLOCK, self.levels(p), p)

    def test_templates_and_normal_files_pass(self):
        for p in [".env.example", ".env.production.example", "lib/main.dart", "src/app.ts",
                  "lib/src/build/widget.dart", "lib/services/local_storage_service.dart",
                  "lib/l10n/locale.json", ".env.local.example", "scripts/deploy.sh"]:
            self.assertEqual(self.levels(p), [], p)

    def test_warned_paths(self):
        for p in ["android/app/google-services.json", "tmp/x.txt", "flutter_build_err.txt"]:
            self.assertEqual(self.levels(p), [guard.WARN], p)

    def test_content(self):
        def levels(path, line):
            return [f.level for f in guard.check_content(path, guard.Change(added=[(1, line, "")]))]

        self.assertEqual(levels("lib/api.dart", "const base = 'http://localhost:3000/api';"), [guard.BLOCK])
        self.assertEqual(levels("lib/api.dart", "const base = 'http://10.0.2.2:3000';"), [guard.BLOCK])
        self.assertEqual(levels("test/api_test.dart", "const base = 'http://localhost:3000';"), [])
        self.assertEqual(levels("docker-compose.yml", "URL: http://localhost:3000"), [guard.BLOCK])
        self.assertEqual(levels("lib/api.dart", "const base = 'https://api.example.com';"), [])
        self.assertEqual(levels("lib/api.dart", "x = 'http://localhost' // commitguard: allow"), [])
        self.assertEqual(levels("a.txt", "-----BEGIN RSA PRIVATE KEY-----"), [guard.BLOCK])
        self.assertEqual(levels("src/cfg.ts", "password = 'hunter2hunter2'"), [guard.WARN])
        self.assertEqual(levels("src/cfg.ts", "password = process.env.DB_PASSWORD"), [])


class LocalOnly(unittest.TestCase):
    def findings(self, path, *lines):
        change = guard.Change(added=[(i, line, "") for i, line in enumerate(lines, 1)])
        return guard.check_content(path, change)

    def levels(self, path, *lines):
        return [f.level for f in self.findings(path, *lines)]

    def test_firewall_scripts_are_blocked_by_content(self):
        bat = [
            ":: Double-click OR run as Administrator to allow phones to reach Nest on port 3000",
            'netsh advfirewall firewall add rule name="API 3000" dir=in action=allow protocol=TCP localport=3000',
        ]
        ps1 = [
            "# Run once as Administrator: right-click PowerShell -> Run as administrator, then:",
            "New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort 3000 -Action Allow",
        ]
        self.assertEqual(self.levels("tools/setup.bat", *bat), [guard.BLOCK, guard.BLOCK])
        self.assertEqual(self.levels("tools/setup.ps1", *ps1), [guard.BLOCK, guard.BLOCK])
        self.assertEqual(self.levels("notes.txt", "netsh interface portproxy add v4tov4 listenport=3000"), [guard.BLOCK])
        self.assertEqual(self.levels("Run", "adb reverse tcp:3000 tcp:3000"), [guard.BLOCK])
        self.assertEqual(self.levels("run.sh", "flutter run -d gu4pylzhceydyhhq"), [guard.BLOCK])
        self.assertEqual(self.levels("fix-hosts.ps1", 'Add-Content C:\\Windows\\System32\\drivers\\etc\\hosts "1.2.3.4 api"'),
                         [guard.BLOCK])

    def test_localhost_in_config_compose_dockerfile_is_blocked(self):
        self.assertEqual(
            self.levels("docker-compose.yml", "NEXT_PUBLIC_API_BASE_URL: ${NEXT_PUBLIC_API_BASE_URL:-http://localhost:3000}"),
            [guard.BLOCK],
        )
        self.assertEqual(
            self.levels("Dockerfile", "#   flutter build web --release --dart-define=API_BASE_URL=http://localhost:3000"),
            [guard.BLOCK],
        )
        self.assertEqual(self.levels("config/app.ini", "API_HOST=localhost:3000"), [guard.BLOCK])
        self.assertEqual(self.levels("package.json", '"proxy": "127.0.0.1:8080",'), [guard.BLOCK])
        self.assertEqual(self.levels("deploy/app.yaml", "API: http://192.168.1.20:8080"), [guard.BLOCK])

    def test_local_urls_are_blocked_everywhere_except_tests_and_docs(self):
        for path, line in [
            (".github/workflows/ci.yml", "DATABASE_URL: postgresql://ci:ci@localhost:5432/ci"),
            ("docker-compose.prod.yml", 'test: ["CMD", "curl", "-f", "http://localhost:3000/health"]'),
            ("deploy/nginx.conf", "proxy_pass http://127.0.0.1:3000;"),
            ("scripts/https.sh", 'echo "Proxy ${DOMAIN} -> http://127.0.0.1:${PORT}"'),
            (".env.local.example", "NEXT_PUBLIC_API_BASE_URL=http://localhost:3000"),
            ("config/app.yaml", "redis: redis://10.1.2.3:6379"),
            ("config/app.yaml", "api: http://172.20.0.5:8080"),
            ("config/app.yaml", "api: http://my-pc.local:8080"),
            ("docker-compose.yml", "API_URL: http://host.docker.internal:3000"),
            ("src/app.module.ts", "              host: 'localhost',"),
        ]:
            found = self.findings(path, line)
            self.assertEqual([f.level for f in found], [guard.BLOCK], f"{path}: {line}")
            self.assertTrue(all(f.local for f in found), path)

        self.assertEqual(self.levels("test/api_test.dart", "final u = 'http://localhost:3000';"), [])

    def test_docs_and_detection_logic_warn_as_local_only(self):
        for path, line in [
            ("docs/setup.md", "REDIS_URL=redis://localhost:6379"),
            ("docs/setup.md", "netsh advfirewall firewall add rule"),
            ("src/url.ts", "  return host !== 'localhost' && !isIp(host);"),
            ("src/paths.ts", "const p = 'D:\\\\data';"),
        ]:
            found = self.findings(path, line)
            self.assertEqual([f.level for f in found], [guard.WARN], f"{path}: {line}")
            self.assertTrue(found[0].local, path)
        self.assertEqual(self.levels("scripts/provision.sh", "ufw allow 443"), [guard.WARN])

    def test_localhost_as_default_value_in_config_is_blocked(self):
        for path, line in [
            ("docker-compose.yml", "API_URL: ${API_URL:-http://localhost:3000}"),
            (".env.example", "API_URL=${API_URL:-http://127.0.0.1:3000}"),
            ("src/config.ts", "export const api = process.env.API_URL ?? 'http://localhost:3000';"),
            ("src/config.py", "API = os.getenv('API_URL', default='http://localhost:8000')"),
            ("config/default.yaml", "  default: http://localhost:8080"),
            ("config/app.json", '  "apiBaseUrl": "http://localhost:3000",'),
        ]:
            found = self.findings(path, line)
            self.assertEqual([f.level for f in found], [guard.BLOCK], f"{path}: {line}")
        [f] = self.findings("src/config.ts", "const api = env.API_URL || 'http://localhost:3000';")
        self.assertIn("default / fallback", f.reason)

    def test_localhost_in_a_comment_or_build_instruction_is_blocked(self):
        for path, line in [
            ("Dockerfile", "# docker build --build-arg API_URL=http://localhost:3000 ."),
            ("Dockerfile", "RUN flutter build web --dart-define=API_BASE_URL=http://localhost:3000"),
            ("src/api.ts", "// e.g. set API_URL=http://localhost:3000 when running"),
            ("Makefile", "\tnpm run build -- --api http://127.0.0.1:3000"),
            ("docker-compose.yml", "      # API_URL: http://192.168.1.5:3000"),
            ("scripts/build.ps1", "REM open http://localhost:8080 after the build"),
        ]:
            found = self.findings(path, line)
            self.assertEqual([f.level for f in found], [guard.BLOCK], f"{path}: {line}")
        [f] = self.findings("Dockerfile", "# docker build --build-arg API_URL=http://localhost:3000 .")
        self.assertIn("comment", f.reason)

    def test_this_computers_paths_and_name(self):
        self.assertEqual(self.levels("build.yaml", "out: C:\\Users\\ahmed\\Desktop\\out"), [guard.BLOCK])
        self.assertEqual(self.levels("scripts/x.sh", "cd /Users/ahmed/work/app"), [guard.BLOCK])
        self.assertEqual(self.levels("start.bat", "cd /d D:\\Project\\backend"), [guard.BLOCK])
        self.assertEqual(self.levels("app.yaml", "image: node:22-alpine", "url: https://api.example.com"), [])
        host = (os.environ.get("COMPUTERNAME") or __import__("socket").gethostname() or "").strip()
        if len(host) >= 4 and host.lower() not in guard._GENERIC_HOSTNAMES:
            self.assertEqual(self.levels("config.yaml", f"host: {host}"), [guard.BLOCK])

    def test_user_rules_and_tracked_audit(self):
        repo = tempfile.mkdtemp(prefix="gitcheck-rules-")
        self.addCleanup(shutil.rmtree, repo, True)
        git("init", "-q", "-b", "main", cwd=repo)
        write(repo, ".commitguard-local", "# mine\nscripts/dev-*.ps1\ncontent: PHONE_SERIAL\n")
        write(repo, "scripts/dev-start.ps1", "npm start\n")
        write(repo, "lib/device.dart", "const id = 'PHONE_SERIAL_123';\n")
        write(repo, "lib/ok.dart", "void main() {}\n")
        found = guard.blocked_paths(guard.run_checks(repo, guard.collect_worktree(repo)))
        self.assertEqual(sorted(found), ["lib/device.dart", "scripts/dev-start.ps1"])

        write(repo, "docker-compose.yml", "services:\n  web:\n    environment:\n      API_URL: http://localhost:3000\n")
        git("add", "-f", "docker-compose.yml", "lib/ok.dart", cwd=repo)
        git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "x", cwd=repo)
        tracked = guard.blocked_paths(guard.run_checks(repo, guard.collect_tracked(repo)))
        self.assertEqual(list(tracked), ["docker-compose.yml"])


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


class DeploySetup(RepoTestCase):
    def found(self):
        return guard.run_checks(self.repo, guard.collect_worktree(self.repo))

    def reasons(self, path):
        return [(f.level, f.reason) for f in self.found() if f.path == path]

    def commit_all(self, msg="base"):
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", msg, cwd=self.repo)

    def test_build_context_that_copies_local_env_files_is_blocked(self):
        write(self.repo, "README.md", "Build with `docker build .`\n")
        self.commit_all()
        write(self.repo, "Dockerfile", "FROM node:22\nWORKDIR /app\nCOPY . .\nRUN npm ci\n")
        [(level, reason)] = self.reasons("Dockerfile")
        self.assertEqual(level, guard.BLOCK)
        self.assertIn(".dockerignore", reason)

        write(self.repo, ".dockerignore", "node_modules\n.env*\n")
        self.assertEqual(self.reasons("Dockerfile"), [])

        for line, expected in [
            ("COPY .env /app/.env", "env file"),
            ("COPY ./backend/.env.production ./", "env file"),
            ("ADD [\".env.local\", \"/app/\"]", "env file"),
            ("COPY build/web /usr/share/nginx/html", "build output"),
            ("COPY dist/ ./dist", "build output"),
            ("COPY config.local.json ./config.json", "local settings"),
        ]:
            write(self.repo, "Dockerfile", f"FROM nginx:alpine\n{line}\n")
            found = [f for f in self.found() if f.path == "Dockerfile"]
            self.assertEqual([f.level for f in found], [guard.BLOCK], line)
            self.assertIn(expected, found[0].reason, line)
            self.assertTrue(found[0].local)

        write(self.repo, "Dockerfile", "FROM node:22 AS build\nCOPY package.json .\nFROM nginx\n"
                                       "COPY --from=build /app/dist /usr/share/nginx/html\nCOPY .env.example ./\n")
        self.assertEqual(self.reasons("Dockerfile"), [])

    def test_new_deploy_setup_that_nothing_references_is_local_only(self):
        write(self.repo, "docker-compose.dev.yml", "services:\n  api:\n    build: .\n")
        write(self.repo, "scripts/start-dev.sh", "#!/bin/sh\nnode server.js\n")
        write(self.repo, "Caddyfile", "example.com {\n  reverse_proxy api:3000\n}\n")
        for path in ("docker-compose.dev.yml", "scripts/start-dev.sh", "Caddyfile"):
            found = [f for f in self.found() if f.path == path]
            self.assertEqual([f.level for f in found], [guard.WARN], path)
            self.assertTrue(found[0].local and "nothing in the project references" in found[0].reason, path)

        write(self.repo, "README.md", "Run the server with `scripts/start-dev.sh`.\n")
        git("add", "README.md", cwd=self.repo)
        git("commit", "-q", "-m", "docs: how to run", cwd=self.repo)
        self.assertEqual(self.reasons("scripts/start-dev.sh"), [])

    def test_new_pipeline_reference_counts_but_new_compose_does_not(self):
        write(self.repo, "Dockerfile.api", "FROM node:22\nCOPY package.json .\n")
        write(self.repo, "compose.yaml", "services:\n  api:\n    build:\n      dockerfile: Dockerfile.api\n")
        self.assertTrue(any("nothing in the project" in r for _, r in self.reasons("Dockerfile.api")))
        write(self.repo, ".github/workflows/deploy.yml", "jobs:\n  x:\n    steps:\n      - run: docker build -f Dockerfile.api .\n")
        self.assertFalse(any("nothing in the project" in r for _, r in self.reasons("Dockerfile.api")))

    def test_second_deploy_path_is_flagged_as_duplicate(self):
        write(self.repo, "Dockerfile", "FROM node:22\nCOPY package.json .\n")
        write(self.repo, "README.md", "docker build .\n")
        self.commit_all()
        write(self.repo, "deploy/Dockerfile.prod", "FROM node:22\nCOPY package.json .\n")
        write(self.repo, "README.md", "docker build .\nProd: docker build -f deploy/Dockerfile.prod .\n")
        [(level, reason)] = self.reasons("deploy/Dockerfile.prod")
        self.assertEqual(level, guard.WARN)
        self.assertIn("already exists (Dockerfile)", reason)

    def test_existing_setup_files_are_not_rechecked(self):
        write(self.repo, "docker-compose.yml", "services:\n  api:\n    image: api\n")
        self.commit_all()
        write(self.repo, "docker-compose.yml", "services:\n  api:\n    image: api:2\n")
        self.assertEqual(self.reasons("docker-compose.yml"), [])


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
        self.assertIn("lib/config.dart", r.local_only)
        self.assertEqual(len(r.skipped), 1)

    def test_local_only_warning_is_excluded_unless_plan_gives_evidence(self):
        write(self.repo, "src/detect.ts", "export const isLocal = (h: string) => h === 'localhost';\n")
        write(self.repo, "src/app.ts", "export const app = 1;\n")
        [r] = committer.run_plan(self.plan([{"message": "feat: detect dev host", "files": ["src/detect.ts", "src/app.ts"]}]))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.files_in("HEAD"), ["src/app.ts"])
        self.assertIn("src/detect.ts", r.local_only)
        self.assertEqual(r.warnings, [])

        evidence = "line 1 compares the hostname to detect a dev host; no address is used"
        [r] = committer.run_plan(self.plan([{"message": "feat: detect dev host", "files": ["src/detect.ts"],
                                             "allow_local": {"src/detect.ts": evidence}}]))
        self.assertEqual(self.files_in("HEAD"), ["src/detect.ts"])
        self.assertIn(evidence, r.local_allowed["src/detect.ts"])

    def test_evidence_never_overrides_a_local_block(self):
        write(self.repo, "src/config.ts", "export const api = process.env.API ?? 'http://localhost:3000';\n")
        [r] = committer.run_plan(self.plan([{"message": "feat: api config", "files": ["src/config.ts"],
                                             "allow_local": {"src/config.ts": "env var overrides it"}}]))
        self.assertEqual(self.log(), ["init"])
        self.assertIn("src/config.ts", r.local_only)
        self.assertTrue(any("allow_local ignored" in w for w in r.warnings))

    def test_unreferenced_deploy_setup_is_not_committed(self):
        write(self.repo, "docker-compose.dev.yml", "services:\n  api:\n    image: api\n")
        write(self.repo, "src/app.ts", "export const app = 1;\n")
        [r] = committer.run_plan(self.plan([{"message": "feat: app and compose", "files": ["src/app.ts", "docker-compose.dev.yml"]}]))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.files_in("HEAD"), ["src/app.ts"])
        self.assertIn("nothing in the project references", r.local_only["docker-compose.dev.yml"])

    def test_rewording_a_flagged_commit_does_not_clear_the_flag(self):
        write(self.repo, "src/net.ts", "export const net = 1;\n")
        [dry] = committer.run_plan(self.plan([{"message": "chore: open ports for my phone", "files": ["src/net.ts"]}]),
                                   dry_run=True)
        self.assertIn("chore: open ports for my phone", dry.flagged)

        [r] = committer.run_plan(self.plan([{"message": "feat: network settings", "files": ["src/net.ts"]}]))
        self.assertEqual(self.log(), ["init"])
        self.assertIn("a new message does not clear that", r.flagged["feat: network settings"])
        self.assertTrue(r.skipped and "flagged local-only" in r.skipped[0])

        [r] = committer.run_plan(self.plan([{"message": "feat: network settings", "files": ["src/net.ts"],
                                             "server_reason": "shared constant used by the deployed API"}]))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.log(), ["feat: network settings", "init"])

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

    def test_local_only_files_are_never_committed_and_get_hidden(self):
        write(self.repo, "scripts/allow-port-3000.bat",
              'netsh advfirewall firewall add rule name="API" dir=in action=allow localport=3000\n')
        write(self.repo, "scripts/setup.ps1", "#Requires -RunAsAdministrator\nNew-NetFirewallRule -LocalPort 3000\n")
        write(self.repo, "scripts/run_local.ps1", "npm start\n")
        write(self.repo, "docker-compose.yml", "services:\n  web:\n    environment:\n      API_URL: http://localhost:3000\n")
        write(self.repo, "src/feature.ts", "export const f = 1;\n")
        plan = self.plan([
            {"message": "feat: add feature", "files": ["src/feature.ts", "docker-compose.yml", "scripts/run_local.ps1"]},
        ])
        exclude_file = os.path.join(self.repo, ".git", "info", "exclude")

        [dry] = committer.run_plan(plan, dry_run=True)
        self.assertEqual(dry.hidden, ["scripts/allow-port-3000.bat", "scripts/run_local.ps1", "scripts/setup.ps1"])
        self.assertFalse(os.path.exists(exclude_file) and "allow-port" in open(exclude_file).read())

        [r] = committer.run_plan(plan)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.files_in("HEAD"), ["src/feature.ts"])
        self.assertIn("docker-compose.yml", r.local_only)
        self.assertEqual(r.hidden, ["scripts/allow-port-3000.bat", "scripts/run_local.ps1", "scripts/setup.ps1"])
        visible = git("status", "--porcelain", "-uall", cwd=self.repo)
        self.assertIn("docker-compose.yml", visible)
        self.assertNotIn("scripts/", visible)
        self.assertEqual(r.left_uncommitted, [])

        [again] = committer.run_plan(self.plan([{"message": "x", "files": ["docker-compose.yml"]}]))
        self.assertEqual(again.hidden, [])

    def test_no_hide_keeps_files_visible(self):
        write(self.repo, "scripts/open-port.sh", "ufw allow 3000\n")
        write(self.repo, "a.py", "x = 1\n")
        [r] = committer.run_plan(self.plan([{"message": "feat: a", "files": ["a.py"]}]), hide_local=False)
        self.assertEqual(r.hidden, [])
        self.assertIn("scripts/open-port.sh", git("status", "--porcelain", "-uall", cwd=self.repo))

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

    def test_local_sounding_commit_is_marked(self):
        from gitcheck import review

        plan = committer.load_plan(self.plan([
            {"message": "chore: scripts to reach the local API from phones", "files": ["src/chart.ts"]},
            {"message": "feat(auth): add login", "files": ["src/auth.ts"]},
        ]))
        shown = review.render(plan, review.dry_run(plan))
        self.assertIn("[1] chore: scripts to reach the local API from phones   -> will be SKIPPED: flagged LOCAL-ONLY"
                      ' (message sounds local-only ("local")) and the plan gives no server_reason', shown)
        self.assertNotIn("[2] feat(auth): add login   ", shown)
        self.assertIn("Total: 2 commit(s), 1 file(s)", shown)

    def test_local_signals_are_listed_as_excluded_never_as_committed_with_warnings(self):
        from gitcheck import review

        write(self.repo, "src/detect.ts", "export const isLocal = (h: string) => h === 'localhost';\n")
        write(self.repo, "compose.yaml", "services:\n  web:\n    environment:\n      API: ${API:-http://localhost:3000}\n")
        plan = committer.load_plan(self.plan([
            {"message": "feat: detect and compose", "files": ["src/detect.ts", "compose.yaml", "src/chart.ts"]},
        ]))
        shown = review.render(plan, review.dry_run(plan))
        self.assertIn("src/detect.ts  (EXCLUDED as local-only: localhost reference", shown)
        self.assertIn("compose.yaml  (EXCLUDED as local-only:", shown)
        self.assertIn("Excluded as local-only (only works on this computer, never committed):", shown)
        self.assertNotIn("WILL be committed", shown)
        self.assertIn("Total: 1 commit(s), 1 file(s)", shown)

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


class Merge(RepoTestCase):
    def setUp(self):
        super().setUp()
        git("checkout", "-q", "-b", "feature/login", cwd=self.repo)
        write(self.repo, "src/login.ts", "export const login = 1;\n")
        self.commit_all("feat: add login")
        write(self.repo, "src/login.ts", "export const login = 2;\n")
        self.commit_all("fix: login value")

    def commit_all(self, msg):
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", msg, cwd=self.repo)

    def on_main(self, path, text, msg):
        git("checkout", "-q", "main", cwd=self.repo)
        write(self.repo, path, text)
        self.commit_all(msg)
        git("checkout", "-q", "feature/login", cwd=self.repo)

    def current(self):
        return git("branch", "--show-current", cwd=self.repo).strip()

    def main_log(self):
        return git("log", "--format=%s", "main", cwd=self.repo).splitlines()

    def test_preview_lists_commits_and_files(self):
        from gitcheck import merger

        p = merger.preview(self.repo)
        self.assertTrue(p.ok, p.problems)
        self.assertEqual((p.source, p.target), ("feature/login", "main"))
        self.assertEqual([c.subject for c in p.commits], ["fix: login value", "feat: add login"])
        self.assertEqual([(f.path, f.status, f.added) for f in p.files], [("src/login.ts", "A", 1)])
        self.assertTrue(p.can_fast_forward)
        shown = merger.render(p, "merge", None, False, False)
        self.assertIn("feature/login  ->  main", shown)
        self.assertIn("Conflicts: none expected", shown)

    def test_merge_commit_keeps_branch_and_returns_to_it(self):
        from gitcheck import merger

        r = merger.merge(merger.preview(self.repo), "merge")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.main_log()[0], "Merge branch 'feature/login' into main")
        self.assertEqual(self.current(), "feature/login")
        self.assertIsNone(merger.preview(self.repo).commits or None)

    def test_squash_with_message_and_delete_branch(self):
        from gitcheck import merger

        self.on_main("docs.md", "x\n", "docs: add docs")
        r = merger.merge(merger.preview(self.repo), "squash", "feat(auth): add login", delete_branch=True)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.main_log()[:2], ["feat(auth): add login", "docs: add docs"])
        self.assertTrue(r.branch_deleted)
        self.assertEqual(self.current(), "main")

    def test_ff_not_possible_when_main_moved(self):
        from gitcheck import merger

        self.on_main("docs.md", "x\n", "docs: add docs")
        p = merger.preview(self.repo)
        self.assertFalse(p.can_fast_forward)
        self.assertTrue(any("fast-forward is not possible" in x for x in merger.check(p, "ff")))
        self.assertEqual(merger.check(p, "merge"), [])

    def test_conflicts_block_merge_without_touching_main(self):
        from gitcheck import merger

        self.on_main("src/login.ts", "export const login = 99;\n", "feat: other login")
        before = self.main_log()
        p = merger.preview(self.repo)
        self.assertEqual(p.conflicts, ["src/login.ts"])
        r = merger.merge(p, "merge")
        self.assertFalse(r.ok)
        self.assertEqual(self.main_log(), before)
        self.assertEqual(self.current(), "feature/login")

    def test_dirty_tree_and_guard_block(self):
        from gitcheck import merger

        write(self.repo, "app.log", "x\n")
        git("add", "-f", "app.log", cwd=self.repo)
        git("commit", "-q", "-m", "oops", cwd=self.repo)
        write(self.repo, "src/login.ts", "export const login = 3;\n")
        problems = " | ".join(merger.preview(self.repo).problems)
        self.assertIn("uncommitted change", problems)
        self.assertIn("guard blocks 1 file(s)", problems)

    def test_failed_merge_hook_restores_everything(self):
        from gitcheck import merger

        self.on_main("docs.md", "x\n", "docs: add docs")
        hook = os.path.join(self.repo, ".git", "hooks", "pre-merge-commit")
        with open(hook, "w", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        before = self.main_log()
        r = merger.merge(merger.preview(self.repo), "merge")
        self.assertFalse(r.ok)
        self.assertEqual(self.main_log(), before)
        self.assertEqual(self.current(), "feature/login")
        self.assertEqual(git("status", "--porcelain", cwd=self.repo).strip(), "")

    def test_behind_origin_is_fast_forwarded_then_pushed(self):
        from gitcheck import merger

        remote = tempfile.mkdtemp(prefix="gitcheck-remote-")
        other = tempfile.mkdtemp(prefix="gitcheck-other-")
        self.addCleanup(shutil.rmtree, remote, True)
        self.addCleanup(shutil.rmtree, other, True)
        git("init", "-q", "--bare", "-b", "main", remote, cwd=self.repo)
        git("remote", "add", "origin", remote, cwd=self.repo)
        git("push", "-q", "-u", "origin", "main", cwd=self.repo)
        git("clone", "-q", remote, other, cwd=self.repo)
        write(other, "remote.md", "x\n")
        git("add", "-A", cwd=other)
        git("-c", "user.email=o@x", "-c", "user.name=o", "commit", "-q", "-m", "docs: from teammate", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)

        p = merger.preview(self.repo, fetch=True)
        self.assertTrue(p.ok, p.problems)
        self.assertEqual((p.base_rev, p.target_behind_upstream), ("origin/main", 1))
        r = merger.merge(p, "merge", push=True)
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.pushed, r.notes)
        self.assertEqual(
            git("log", "--format=%s", "main", cwd=remote).splitlines()[:2],
            ["Merge branch 'feature/login' into main", "docs: from teammate"],
        )

    def test_review_other_changes_strategy_then_yes(self):
        from gitcheck import merger, review

        opts = merger.MergeOptions()
        answers = iter(["o", "s", "s", "o", "d", "y"])
        decision, _ = review.review_merge(
            merger.preview(self.repo), opts, lambda t: merger.preview(self.repo, None, t),
            ask=lambda _: next(answers), out=lambda _: None,
        )
        self.assertEqual(decision, review.APPROVED)
        self.assertEqual((opts.strategy, opts.delete_branch), ("squash", True))

    def test_cli_refuses_without_approval_when_not_interactive(self):
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitcheck.py"),
             "merge", "-C", self.repo],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("feature/login  ->  main", r.stdout)
        self.assertEqual(self.main_log(), ["init"])


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
        self.skill_dir = os.path.join(self.tmp, "skills")
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
        for name in skill.SKILL_NAMES:
            self.assertEqual(skill.state(name, self.skill_dir), "ok")
            with open(os.path.join(self.skill_dir, name, "SKILL.md"), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("{{", text)
            self.assertIn(hooks.ENTRY_SCRIPT.replace("\\", "/"), text)
            self.assertIn(f"name: {name}", text)
        self.assertTrue(hooks.hooks_current(self.a) and hooks.hooks_current(self.b))

        installer.setup([self.a], install_hooks=False, assume_yes=True, skill_target=self.skill_dir)
        self.assertEqual(len(config.projects()), 2)

        self.assertEqual(installer.doctor(self.skill_dir), 0)
        shutil.rmtree(self.b)
        self.assertEqual(installer.doctor(self.skill_dir), 1)

        config.remove_projects([self.b])
        self.assertEqual(config.projects(), [self.a])

        installer.uninstall(keep_config=False, skill_target=self.skill_dir)
        self.assertEqual([skill.state(n, self.skill_dir) for n in skill.SKILL_NAMES], ["missing", "missing"])
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
