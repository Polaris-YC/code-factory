"""Git worktree lifecycle management, diff inspection, and branch integration."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git subprocess fails."""


class MergeConflictError(GitError):
    """Raised when merging a task branch causes merge conflicts."""

    def __init__(self, branch: str, files: list[str], details: str = "") -> None:
        self.branch = branch
        self.files = files
        super().__init__(
            f"Merge conflict while integrating '{branch}': {', '.join(files)}. {details}".strip()
        )


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    branch: str
    path: Path
    base_ref: str


_EPHEMERAL_SEGMENTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _is_ephemeral_artifact(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    if any(seg in _EPHEMERAL_SEGMENTS for seg in parts):
        return True
    return rel_path.endswith((".pyc", ".pyo"))


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc


class GitWorktreeManager:
    def __init__(self, repo_root: Path, worktree_root: str = ".code-factory/worktrees") -> None:
        self.repo_root = repo_root.resolve()
        wt_path = Path(worktree_root)
        self.worktree_root = (
            wt_path if wt_path.is_absolute() else (self.repo_root / wt_path)
        ).resolve()

    def current_branch(self) -> str:
        proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root)
        return proc.stdout.strip()

    def resolve_ref(self, ref: str) -> str:
        proc = _git(["rev-parse", ref], cwd=self.repo_root)
        return proc.stdout.strip()

    def ensure_branch_exists(self, branch: str) -> bool:
        proc = _git(["rev-parse", "--verify", branch], cwd=self.repo_root, check=False)
        return proc.returncode == 0

    def create_worktree(
        self,
        run_id: str,
        task_id: str,
        base_ref: str,
    ) -> WorktreeHandle:
        safe_task = task_id.replace("/", "-")
        branch = f"cf/{run_id}/{safe_task}"
        wt_path = self.worktree_root / run_id / safe_task
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        if wt_path.exists():
            self.remove_worktree(wt_path)

        if self.ensure_branch_exists(branch):
            _git(["branch", "-D", branch], cwd=self.repo_root)

        _git(
            ["worktree", "add", "-b", branch, str(wt_path), base_ref],
            cwd=self.repo_root,
        )
        return WorktreeHandle(
            task_id=task_id,
            branch=branch,
            path=wt_path,
            base_ref=base_ref,
        )

    def remove_worktree(self, worktree_path: Path, force: bool = True) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))
        proc = _git(args, cwd=self.repo_root, check=False)
        if proc.returncode != 0 and worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
            _git(["worktree", "prune"], cwd=self.repo_root, check=False)

    def changed_files(self, worktree_path: Path, base_ref: str) -> list[str]:
        """Return all paths changed relative to base_ref, including untracked files."""
        files: set[str] = set()
        committed = _git(
            ["diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=worktree_path,
            check=False,
        )
        if committed.returncode == 0:
            for line in committed.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())

        unstaged = _git(["diff", "--name-only"], cwd=worktree_path, check=False)
        if unstaged.returncode == 0:
            for line in unstaged.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())

        staged = _git(["diff", "--cached", "--name-only"], cwd=worktree_path, check=False)
        if staged.returncode == 0:
            for line in staged.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())

        untracked = _git(
            ["ls-files", "--others", "--exclude-standard"],
            cwd=worktree_path,
            check=False,
        )
        if untracked.returncode == 0:
            for line in untracked.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())

        return sorted(f for f in files if not _is_ephemeral_artifact(f))

    def unified_diff(self, worktree_path: Path, base_ref: str) -> str:
        """Return unified diff vs base_ref, including staged/unstaged changes."""
        parts: list[str] = []
        base_diff = _git(["diff", base_ref], cwd=worktree_path, check=False)
        if base_diff.returncode == 0 and base_diff.stdout:
            parts.append(base_diff.stdout)

        untracked = _git(
            ["ls-files", "--others", "--exclude-standard"],
            cwd=worktree_path,
            check=False,
        )
        if untracked.returncode == 0:
            for rel in untracked.stdout.splitlines():
                rel = rel.strip()
                if not rel or _is_ephemeral_artifact(rel):
                    continue
                target = worktree_path / rel
                if target.is_file():
                    try:
                        text = target.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    added = "\n".join(f"+{line}" for line in text.splitlines())
                    parts.append(
                        f"diff --git a/{rel} b/{rel}\n"
                        f"new file mode 100644\n"
                        f"--- /dev/null\n"
                        f"+++ b/{rel}\n"
                        f"@@ -0,0 +1,{len(text.splitlines())} @@\n"
                        f"{added}\n"
                    )
        return "\n".join(parts)

    def commit_all(self, worktree_path: Path, message: str) -> str:
        """Stage all changes in worktree and commit if dirty; return HEAD sha."""
        status = _git(["status", "--porcelain"], cwd=worktree_path)
        if status.stdout.strip():
            _git(["add", "-A"], cwd=worktree_path)
            _git(
                [
                    "-c",
                    "user.name=Code Factory",
                    "-c",
                    "user.email=code-factory@local",
                    "commit",
                    "-m",
                    message,
                ],
                cwd=worktree_path,
            )
        return _git(["rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    def create_integration_worktree(
        self,
        run_id: str,
        base_branch: str,
    ) -> WorktreeHandle:
        branch = f"cf/{run_id}/integrate"
        wt_path = self.worktree_root / run_id / "_integrate"
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        if wt_path.exists():
            self.remove_worktree(wt_path)
        if self.ensure_branch_exists(branch):
            _git(["branch", "-D", branch], cwd=self.repo_root)
        _git(
            ["worktree", "add", "-b", branch, str(wt_path), base_branch],
            cwd=self.repo_root,
        )
        return WorktreeHandle(
            task_id="_integrate",
            branch=branch,
            path=wt_path,
            base_ref=base_branch,
        )

    def merge_branch(
        self,
        integration_worktree: Path,
        source_branch: str,
        message: str,
    ) -> str:
        proc = _git(
            [
                "-c",
                "user.name=Code Factory",
                "-c",
                "user.email=code-factory@local",
                "merge",
                "--no-ff",
                "-m",
                message,
                source_branch,
            ],
            cwd=integration_worktree,
            check=False,
        )
        if proc.returncode != 0:
            conflicts_proc = _git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=integration_worktree,
                check=False,
            )
            conflict_files = [
                line.strip()
                for line in conflicts_proc.stdout.splitlines()
                if line.strip()
            ]
            _git(["merge", "--abort"], cwd=integration_worktree, check=False)
            raise MergeConflictError(
                branch=source_branch,
                files=conflict_files,
                details=(proc.stderr or proc.stdout).strip(),
            )
        return _git(["rev-parse", "HEAD"], cwd=integration_worktree).stdout.strip()

    def merge_into_base(self, integration_branch: str, base_branch: str) -> str:
        """Merge verified integration branch into base_branch (used only in --dark mode)."""
        _git(["checkout", base_branch], cwd=self.repo_root)
        proc = _git(
            [
                "-c",
                "user.name=Code Factory",
                "-c",
                "user.email=code-factory@local",
                "merge",
                "--no-ff",
                "-m",
                f"code-factory(dark): merge {integration_branch}",
                integration_branch,
            ],
            cwd=self.repo_root,
            check=False,
        )
        if proc.returncode != 0:
            _git(["merge", "--abort"], cwd=self.repo_root, check=False)
            raise GitError(
                f"Failed to merge '{integration_branch}' into '{base_branch}': "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return _git(["rev-parse", "HEAD"], cwd=self.repo_root).stdout.strip()

    def create_pr(
        self,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Push integration branch and create a GitHub PR via `gh pr create`."""
        _git(["push", "-u", "origin", branch], cwd=self.repo_root)
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"gh pr create failed: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip()
