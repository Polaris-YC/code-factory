"""Tests for bundle discovery and the version and cleanup commands."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from code_factory import __version__
from code_factory.cli import main
from code_factory.core.installer import install_bundle, resolve_bundle_root
from code_factory.core.worktree import GitWorktreeManager


def _make_bundle(root: Path) -> Path:
    skill = root / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    (root / "plugin.json").write_text("{}", encoding="utf-8")
    return root


class TestInstallerAndVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.prefix = self.tmp / "prefix"
        self.shared_root = self.prefix / "share" / "code-factory"
        self.enterContext(patch("code_factory.core.installer.sys.prefix", str(self.prefix)))

    def test_version_flags(self) -> None:
        for flag in ("-V", "--version"):
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exc:
                    main([flag])
                self.assertEqual(exc.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), f"cf {__version__}")

    def test_resolve_prefers_checkout_and_falls_back_to_shared_data(self) -> None:
        source = _make_bundle(self.tmp / "source")
        shared = _make_bundle(self.shared_root)
        self.assertEqual(resolve_bundle_root(source), source)
        self.assertEqual(resolve_bundle_root(self.tmp / "site-packages"), shared)

    def test_missing_bundle_includes_install_instructions(self) -> None:
        source = self.tmp / "source"
        (source / "skills").mkdir(parents=True)
        with self.assertRaisesRegex(FileNotFoundError, "Reinstall Code Factory") as exc:
            resolve_bundle_root(source)
        self.assertIn(str(source), str(exc.exception))
        self.assertIn(str(self.shared_root), str(exc.exception))

    def test_install_bundle_dry_run(self) -> None:
        bundle = _make_bundle(self.shared_root)
        workspace = self.tmp / "workspace"
        skill = bundle / "skills" / "example"
        actions = install_bundle(
            package_root=self.tmp / "site-packages",
            scope="workspace",
            workspace=workspace,
            copy=True,
            dry_run=True,
        )
        self.assertEqual(
            [(action.target, action.source, action.destination, action.mode) for action in actions],
            [
                ("antigravity", bundle, workspace / ".agents/plugins/code-factory", "copy"),
                ("claude", skill, workspace / ".claude/skills/example", "copy"),
                ("codex", skill, workspace / ".codex/skills/example", "copy"),
            ],
        )
        self.assertFalse(workspace.exists())


class TestClean(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.git("init", "-q", "-b", "main")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "initial", "--allow-empty",
        )
        config = self.repo / ".code-factory" / "factory.toml"
        config.parent.mkdir()
        config.write_text('[factory]\nworktree_root = "factory worktrees"\n', encoding="utf-8")
        self.manager = GitWorktreeManager(self.repo, "factory worktrees")
        self.clean_args = ["clean", "--repo", str(self.repo)]

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout

    def test_clean_dry_run_leaves_worktrees_and_registrations(self) -> None:
        worktree = self.manager.create_worktree("run1", "task", "main")
        integration = self.manager.create_integration_worktree("run1", "main")
        before = self.git("worktree", "list", "--porcelain")

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*self.clean_args, "--dry-run"]), 0)

        for handle in (worktree, integration):
            self.assertIn(f"Would remove {handle.path}", output.getvalue())
            self.assertTrue(handle.path.is_dir())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before)

    def test_clean_removes_clean_and_missing_worktrees_and_preserves_other_data(self) -> None:
        clean = self.manager.create_worktree("run1", "clean", "main")
        missing = self.manager.create_worktree("run1", "missing", "main")
        shutil.rmtree(missing.path)
        dirty = self.manager.create_worktree("run1", "dirty", "main")
        (self.repo / ".git/info/exclude").write_text("local.env\n", encoding="utf-8")
        (dirty.path / "local.env").write_text("local data\n", encoding="utf-8")
        locked = self.manager.create_worktree("run1", "locked", "main")
        self.git("worktree", "lock", "--reason", "keep", str(locked.path))
        foreign = clean.path.with_name("foreign")
        self.git("worktree", "add", "-qb", "foreign", str(foreign))

        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(self.clean_args), 1)

        self.assertIn(f"Removed {clean.path}", output.getvalue())
        self.assertIn(str(dirty.path), errors.getvalue())
        self.assertIn(f"Skipping {locked.path}: worktree is locked.", errors.getvalue())
        self.assertFalse(clean.path.exists())
        listing = self.git("worktree", "list", "--porcelain")
        self.assertNotIn(f"worktree {clean.path}\n", listing)
        self.assertNotIn(f"worktree {missing.path}\n", listing)
        self.assertEqual((dirty.path / "local.env").read_text(encoding="utf-8"), "local data\n")
        self.assertTrue(locked.path.is_dir())
        self.assertTrue(foreign.is_dir())
        self.assertTrue(self.manager.ensure_branch_exists(clean.branch))
