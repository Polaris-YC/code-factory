"""Integration tests for GitWorktreeManager against real temporary Git repos."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from code_factory.core.worktree import GitWorktreeManager, MergeConflictError


def _init_temp_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    (path / "README.md").write_text("# Temp Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)


class TestWorktreeManager(unittest.TestCase):
    def test_parallel_worktrees_clean_integration_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_temp_repo(repo)

            mgr = GitWorktreeManager(repo)
            wt1 = mgr.create_worktree("run1", "task-a", "main")
            wt2 = mgr.create_worktree("run1", "task-b", "main")

            (wt1.path / "a.txt").write_text("hello from a\n", encoding="utf-8")
            (wt2.path / "b.txt").write_text("hello from b\n", encoding="utf-8")

            self.assertEqual(mgr.changed_files(wt1.path, "main"), ["a.txt"])
            self.assertIn("+hello from a", mgr.unified_diff(wt1.path, "main"))

            mgr.commit_all(wt1.path, "add a")
            mgr.commit_all(wt2.path, "add b")

            integ = mgr.create_integration_worktree("run1", "main")
            mgr.merge_branch(integ.path, wt1.branch, "merge a")
            mgr.merge_branch(integ.path, wt2.branch, "merge b")

            self.assertTrue((integ.path / "a.txt").is_file())
            self.assertTrue((integ.path / "b.txt").is_file())

            mgr.remove_worktree(wt1.path)
            mgr.remove_worktree(wt2.path)
            mgr.remove_worktree(integ.path)
            self.assertFalse(wt1.path.exists())
            self.assertFalse(wt2.path.exists())

    def test_merge_conflict_raises_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_temp_repo(repo)

            mgr = GitWorktreeManager(repo)
            wt1 = mgr.create_worktree("run2", "t1", "main")
            wt2 = mgr.create_worktree("run2", "t2", "main")

            (wt1.path / "README.md").write_text("# Version One\n", encoding="utf-8")
            (wt2.path / "README.md").write_text("# Version Two\n", encoding="utf-8")
            mgr.commit_all(wt1.path, "edit 1")
            mgr.commit_all(wt2.path, "edit 2")

            integ = mgr.create_integration_worktree("run2", "main")
            mgr.merge_branch(integ.path, wt1.branch, "merge t1")
            with self.assertRaises(MergeConflictError) as ctx:
                mgr.merge_branch(integ.path, wt2.branch, "merge t2")
            self.assertIn("README.md", ctx.exception.files)


if __name__ == "__main__":
    unittest.main()
