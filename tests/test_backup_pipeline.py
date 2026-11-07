import ast
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_html", ROOT / "scripts/render_html.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


class BackupPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "runner"
        self.repo.mkdir()
        self.git("init", "--bare", str(self.remote))
        self.git("init", "--initial-branch=seed")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", self.remote.as_uri())

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def write(self, path, contents="example"):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    def script(self, command, success=True):
        result = subprocess.run(["python3", str(ROOT / "scripts/backup_git.py"), command], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def seed(self):
        self.write("backups/site/old/page.html")
        self.write("backups/native/old/archive.zip", "legacy archive")
        self.write("backups/.staging/old/temporary")
        self.write("backups/notion-json/blocks/old.json")
        self.write("backups/manifests/coverage_report.json", json.dumps({"quality": {"failures": []}, "md_count": 100}))
        self.git("add", ".")
        self.git("commit", "-qm", "old backup")
        self.base = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "HEAD:refs/heads/notion-backups")
        # Simulate a clean main checkout with backups ignored.
        self.git("checkout", "--orphan", "main")
        self.git("rm", "-rf", ".")
        self.write(".gitignore", "backups/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "code branch")

    def fresh(self):
        self.write("backups/official/new/page.md", "# New page")
        self.write("backups/manifests/coverage_report.json", json.dumps({"quality": {"failures": []}}))
        render.main(self.repo)

    def test_restore_and_two_publications_preserve_history_and_archives(self):
        self.seed()
        self.script("restore")
        self.assertFalse((self.repo / "backups/site").exists())
        self.assertFalse((self.repo / "backups/native").exists())
        self.assertTrue((self.repo / "backups/manifests/coverage_report.json").exists())
        self.fresh()
        self.write("backups/.staging/new/unwanted.zip")
        self.script("publish")
        self.git("fetch", "origin", "notion-backups")
        paths = self.git("ls-tree", "-r", "--name-only", "FETCH_HEAD")
        self.assertNotIn("site/old", paths)
        self.assertNotIn(".staging", paths)
        self.assertIn("native/old/archive.zip", paths)
        self.assertIn("notion-json/blocks/old.json", paths)
        self.assertEqual(self.git("rev-parse", "FETCH_HEAD^"), self.base)
        self.assertEqual(self.git("show", self.base + ":backups/site/old/page.html"), "example")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.script("restore")
        self.write("backups/official/new/page.md", "# Changed page")
        render.main(self.repo)
        self.script("publish")
        self.git("fetch", "origin", "notion-backups")
        self.assertEqual(self.git("show", "FETCH_HEAD:backups/official/new/page.md"), "# Changed page")

    def test_export_checkpoint_survives_failed_run_without_changing_backup(self):
        self.seed()
        self.script("restore-state")
        pending = {"pending": {"task_id": "task-1", "space_id": "workspace",
                               "requested_at": "2026-11-05T00:00:00Z"}}
        self.write("backups/.state/workspace-export.json", json.dumps(pending))
        self.script("save-state")
        self.assertTrue(self.git("ls-remote", "origin", "refs/heads/notion-backups").startswith(self.base))
        (self.repo / "backups/.state/workspace-export.json").unlink()
        self.script("restore-state")
        self.assertEqual(json.loads((self.repo / "backups/.state/workspace-export.json").read_text()), pending)
        self.script("complete-state")
        self.script("restore-state")
        self.assertEqual(json.loads((self.repo / "backups/.state/workspace-export.json").read_text()), {"pending": None})
        self.assertTrue(self.git("ls-remote", "origin", "refs/heads/notion-backups").startswith(self.base))

    def test_first_publication(self):
        self.write("code.txt")
        self.git("add", ".")
        self.git("commit", "-qm", "main")
        self.script("restore")
        self.fresh()
        self.script("publish")
        self.assertTrue(self.git("ls-remote", "origin", "refs/heads/notion-backups"))

    def test_failed_quality_and_concurrent_update_do_not_replace_backup(self):
        self.seed()
        self.script("restore")
        self.fresh()
        self.write("backups/manifests/coverage_report.json", json.dumps({"quality": {"failures": ["drop"]}}))
        self.script("publish", success=False)
        self.assertTrue(self.git("ls-remote", "origin", "refs/heads/notion-backups").startswith(self.base))
        self.fresh()
        self.script("publish")
        # A second attempt using the same old base must fail fast-forward checks.
        self.write("backups/official/new/page.md", "stale writer")
        self.script("publish", success=False)

    def test_api_promotion_preserves_official_quality_baseline(self):
        # Load just the filesystem promotion helper; this test needs no API SDKs.
        module = ast.parse((ROOT / "scripts/export_official.py").read_text())
        helper = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "promote_staging")
        namespace = {"Path": Path, "shutil": shutil}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), "export_official.py", "exec"), namespace)
        self.write("backups/manifests/coverage_report.json", '{"md_count": 3928}')
        self.write("backups/manifests/workspace_export.json", '{"export_mode": "workspace"}')
        self.write("backups/.staging/api/backups/manifests/manifest.json", '{"object_count": 20}')
        namespace["promote_staging"](self.repo / "backups/.staging/api/backups", self.repo / "backups")
        reports = self.repo / "backups/manifests"
        self.assertEqual(json.loads((reports / "coverage_report.json").read_text())["md_count"], 3928)
        self.assertTrue((reports / "workspace_export.json").exists())
        self.assertEqual(json.loads((reports / "manifest.json").read_text())["object_count"], 20)

    def test_render_replaces_old_output_but_preserves_it_on_failure(self):
        self.fresh()
        shutil.rmtree(self.repo / "backups/official/new")
        self.write("backups/official/next/page.md", "# Next")
        render.main(self.repo)
        self.assertFalse((self.repo / "backups/site/new").exists())
        expected = (self.repo / "backups/site/index.html").read_text()
        (self.repo / "backups/official/next/page.md").write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            render.main(self.repo)
        self.assertEqual((self.repo / "backups/site/index.html").read_text(), expected)
        shutil.rmtree(self.repo / "backups/official")
        with self.assertRaises(RuntimeError):
            render.main(self.repo)
        self.assertEqual((self.repo / "backups/site/index.html").read_text(), expected)


if __name__ == "__main__":
    unittest.main()
