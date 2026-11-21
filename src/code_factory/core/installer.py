"""Multi-harness installer for Antigravity, Claude Code, and Codex skill bundles."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallAction:
    target: str
    source: Path
    destination: Path
    mode: str


def resolve_bundle_root(package_root: Path) -> Path:
    """Find the bundle in a source checkout or the active installation."""
    package_root = package_root.resolve()
    shared_root = Path(sys.prefix) / "share" / "code-factory"
    for root in (package_root, shared_root):
        if (root / "skills").is_dir() and (root / "plugin.json").is_file():
            return root
    raise FileNotFoundError(
        f"Code Factory bundle not found in {package_root} or {shared_root}. "
        "Reinstall Code Factory from a wheel, or run 'uv run cf install' "
        "from a complete source checkout."
    )


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
    pkg = resolve_bundle_root(package_root)
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
        elif harness in {"claude", "codex"}:
            base = (home if scope == "global" else ws) / f".{harness}" / "skills"
            for skill in sorted((pkg / "skills").iterdir()):
                if skill.is_dir() and (skill / "SKILL.md").is_file():
                    dst = base / skill.name
                    mode = _link_or_copy(skill, dst, copy=copy, dry_run=dry_run)
                    actions.append(InstallAction(harness, skill, dst, mode))
        else:
            raise ValueError(f"Unsupported install target: '{harness}'")

    return actions
