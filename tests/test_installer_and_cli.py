import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import Distribution
from pathlib import Path
from unittest.mock import patch

from code_factory import __version__
from code_factory.cli import main
from code_factory.core.installer import install_bundle, resolve_bundle_root
from code_factory.core.worktree import GitWorktreeManager


def make_bundle(root: Path) -> Path:
    (root / "skills/example").mkdir(parents=True)
    (root / "skills/example/SKILL.md").touch()
    (root / "plugin.json").touch()
    return root


class TestInstallerAndVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        prefix = self.tmp / "prefix"
        self.shared = prefix / "share/code-factory"
        self.enterContext(patch("sys.prefix", str(prefix)))

    def test_version_flags(self) -> None:
        for flag in ("-V", "--version"):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exc:
                main([flag])
            self.assertEqual(exc.exception.code, 0)
            self.assertEqual(output.getvalue(), f"cf {__version__}\n")

    def test_resolve_bundle_root(self) -> None:
        source = self.tmp / "source"
        with self.assertRaisesRegex(FileNotFoundError, "Reinstall Code Factory"):
            resolve_bundle_root(source)
        make_bundle(self.shared)
        self.assertEqual(resolve_bundle_root(source), self.shared)
        self.assertEqual(resolve_bundle_root(make_bundle(source)), source)

    def test_install_bundle_dry_run(self) -> None:
        bundle = make_bundle(self.shared)
        workspace = self.tmp / "workspace"
        actions = install_bundle(
            package_root=self.tmp, workspace=workspace, scope="workspace", copy=True, dry_run=True,
        )
        self.assertEqual([a.target for a in actions], ["antigravity", "claude", "codex"])
        self.assertTrue(all(a.source.is_relative_to(bundle) and a.mode == "copy" for a in actions))
        self.assertFalse(workspace.exists())

    def test_install_uses_the_imported_distribution_bundle(self) -> None:
        make_bundle(self.shared)
        site = self.tmp / "lib/python3.12/site-packages"
        info = site / "code_factory-0.1.0.dist-info"
        info.mkdir(parents=True)
        bundle = make_bundle(self.tmp / "share/code-factory")
        (info / "RECORD").write_text("code_factory/cli.py,,\n../../../share/code-factory/plugin.json,,\n")
        with (
            patch("code_factory.cli.__file__", str(site / "code_factory/cli.py")),
            patch("code_factory.cli.distributions", return_value=[Distribution.at(info)]),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["install", "--target", "antigravity", "--dry-run"]), 0)
        self.assertIn(f"symlink: {bundle} ->", output.getvalue())


class TestClean(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-qm", "initial")
        config = self.repo / ".code-factory/factory.toml"
        config.parent.mkdir()
        config.write_text('[factory]\nworktree_root = "factory worktrees"\n')
        self.manager = GitWorktreeManager(self.repo, "factory worktrees")
        self.args = ["--repo", str(self.repo)]

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)

    def clean(self, *options: str, status: int = 0) -> tuple[str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["clean", *self.args, *options]), status, errors.getvalue())
        return output.getvalue(), errors.getvalue()

    def test_dry_run_preserves_worktrees_and_registrations(self) -> None:
        task = self.manager.create_worktree("run1", "task", "main").path
        integration = self.manager.create_integration_worktree("run1", "main").path
        before = self.git("worktree", "list", "--porcelain")
        output, _ = self.clean("--dry-run")
        for path in (task, integration):
            self.assertIn(f"Would remove {path}", output)
            self.assertTrue(path.is_dir())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before)

    def test_remove_and_prune_preserve_other_worktrees(self) -> None:
        clean = self.manager.create_worktree("run1", "clean", "main")
        missing = self.manager.create_worktree("run1", "missing", "main").path
        dirty = self.manager.create_worktree("run1", "dirty", "main").path
        locked = self.manager.create_worktree("run1", "locked", "main").path
        foreign = clean.path.with_name("foreign")
        self.git("worktree", "add", "-qb", "foreign", str(foreign))
        self.git("worktree", "lock", str(locked))
        (self.repo / ".git/info/exclude").write_text("local.env\n")
        (dirty / "local.env").touch()
        shutil.rmtree(missing)
        self.clean(status=1)
        listing = self.git("worktree", "list", "--porcelain")
        for path in (clean.path, missing):
            self.assertNotIn(f"worktree {path}\n", listing)
            self.assertFalse(path.exists())
        self.assertTrue(self.manager.ensure_branch_exists(clean.branch))
        for path in (dirty / "local.env", locked, foreign):
            self.assertTrue(path.exists())

    def test_cleanup_skips_active_run_and_cleans_stopped_escalated_run(self) -> None:
        path = self.manager.create_integration_worktree("direct/run", "main").path
        state = self.repo / ".code-factory/runs/direct/run/state.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"integrated": false}')

        with redirect_stderr(io.StringIO()) as errors, patch(
            "code_factory.cli._cmd_run", side_effect=lambda _: main(["clean", *self.args]),
        ):
            self.assertEqual(main(["run", *self.args, "--dag", "unused.json"]), 1)
        self.assertIn("a run or cleanup is active", errors.getvalue())
        self.assertTrue(path.exists())

        # Once the escalated run has exited and released its lock, clean removes it.
        self.clean()
        self.assertFalse(path.exists())
