"""Multi-harness installer for Antigravity, Claude Code, and Codex skill bundles."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallAction:
    target: str
    source: Path
    destination: Path
    mode: str


def _link_or_copy(src: Path, dst: Path, *, copy: bool, dry_run: bool) -> str:
    mode = "copy" if copy else "symlink"
    if dry_run:
        return mode
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    if copy:
        shutil.copytree(src, dst)
    else:
        dst.symlink_to(src.resolve())
    return mode


def install_bundle(
    *,
    package_root: Path,
    target: str = "all",
    scope: str = "global",
    workspace: Path | None = None,
    copy: bool = False,
    dry_run: bool = False,
) -> list[InstallAction]:
    """Install Code Factory plugin/skills into Antigravity, Claude Code, and/or Codex."""
    pkg = package_root.resolve()
    skills_dir = pkg / "skills"
    actions: list[InstallAction] = []

    targets = ["antigravity", "claude", "codex"] if target == "all" else [target]
    ws = (workspace or Path.cwd()).resolve()
    home = Path.home()

    for harness in targets:
        if harness == "antigravity":
            dst = (
                home / ".gemini" / "config" / "plugins" / "code-factory"
                if scope == "global"
                else ws / ".agents" / "plugins" / "code-factory"
            )
            mode = _link_or_copy(pkg, dst, copy=copy, dry_run=dry_run)
            actions.append(InstallAction("antigravity", pkg, dst, mode))
        elif harness == "claude":
            base = (
                home / ".claude" / "skills"
                if scope == "global"
                else ws / ".claude" / "skills"
            )
            if skills_dir.is_dir():
                for skill in sorted(skills_dir.iterdir()):
                    if skill.is_dir() and (skill / "SKILL.md").is_file():
                        dst = base / skill.name
                        mode = _link_or_copy(skill, dst, copy=copy, dry_run=dry_run)
                        actions.append(InstallAction("claude", skill, dst, mode))
        elif harness == "codex":
            base = (
                home / ".codex" / "skills"
                if scope == "global"
                else ws / ".codex" / "skills"
            )
            if skills_dir.is_dir():
                for skill in sorted(skills_dir.iterdir()):
                    if skill.is_dir() and (skill / "SKILL.md").is_file():
                        dst = base / skill.name
                        mode = _link_or_copy(skill, dst, copy=copy, dry_run=dry_run)
                        actions.append(InstallAction("codex", skill, dst, mode))
        else:
            raise ValueError(f"Unsupported install target: '{harness}'")

    return actions
