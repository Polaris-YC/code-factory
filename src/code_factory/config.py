"""Layered TOML configuration loader for Code Factory."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_factory.core.models import FactoryMode


@dataclass(frozen=True)
class GateCommandConfig:
    name: str
    cmd: str
    timeout: int = 180


@dataclass(frozen=True)
class SlopGateConfig:
    enabled: bool = True
    max_diff_lines: int = 800
    count_deletions_in_diff_budget: bool = False
    max_comment_ratio: float = 0.35
    enforce_owned_paths: bool = True
    forbid_patterns: list[str] = field(
        default_factory=lambda: [
            r"TODO\(agent\)",
            r"FIXME\(agent\)",
            r'NotImplementedError\("todo',
        ]
    )


@dataclass(frozen=True)
class ArchRuleConfig:
    from_module: str
    forbid: list[str] = field(default_factory=list)
    forbid_calls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchGateConfig:
    enabled: bool = False
    src_root: str = "src"
    rules: list[ArchRuleConfig] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionGateConfig:
    enabled: bool = True
    default_deny: bool = True
    protected_paths: list[str] = field(
        default_factory=lambda: [
            ".code-factory/**",
            ".github/**",
        ]
    )
    allow_delete: bool = True
    allow_symlinks: bool = False


@dataclass(frozen=True)
class FactoryConfig:
    mode: FactoryMode = FactoryMode.SOFTWARE
    max_workers: int = 3
    max_gate_retries: int = 3
    max_review_rounds: int = 2
    cross_model_review: bool = True
    task_timeout_seconds: int = 600
    reviewer_timeout_seconds: int = 180
    worktree_root: str = ".code-factory/worktrees"
    routing: dict[str, list[str]] = field(
        default_factory=lambda: {
            "planner": ["codex", "claude", "agy"],
            "worker": ["codex", "claude", "agy"],
            "reviewer": ["claude", "agy", "codex"],
        }
    )
    adapters: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "codex": {
                "type": "codex",
                "command": "codex",
                "sandbox": "workspace-write",
                "approve_for_me": True,
            },
            "claude": {
                "type": "claude",
                "command": "claude",
                "allowed_tools": ["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
            },
            "agy": {
                "type": "agy",
                "command": "agy",
                "mode": "accept-edits",
                "sandbox": True,
            },
        }
    )
    gate_commands: list[GateCommandConfig] = field(default_factory=list)
    slop: SlopGateConfig = field(default_factory=SlopGateConfig)
    arch: ArchGateConfig = field(default_factory=ArchGateConfig)
    permissions: PermissionGateConfig = field(default_factory=PermissionGateConfig)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml_if_exists(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def load_config(
    repo_root: Path | None = None,
    global_config_path: Path | None = None,
) -> FactoryConfig:
    """Load merged configuration: defaults < global config < repo config."""
    defaults = FactoryConfig()
    raw: dict[str, Any] = {
        "factory": {
            "mode": defaults.mode.value,
            "max_workers": defaults.max_workers,
            "max_gate_retries": defaults.max_gate_retries,
            "max_review_rounds": defaults.max_review_rounds,
            "cross_model_review": defaults.cross_model_review,
            "task_timeout_seconds": defaults.task_timeout_seconds,
            "reviewer_timeout_seconds": defaults.reviewer_timeout_seconds,
            "worktree_root": defaults.worktree_root,
        },
        "routing": dict(defaults.routing),
        "adapters": {k: dict(v) for k, v in defaults.adapters.items()},
        "gates": {
            "commands": [],
            "slop": {
                "enabled": defaults.slop.enabled,
                "max_diff_lines": defaults.slop.max_diff_lines,
                "count_deletions_in_diff_budget": defaults.slop.count_deletions_in_diff_budget,
                "max_comment_ratio": defaults.slop.max_comment_ratio,
                "enforce_owned_paths": defaults.slop.enforce_owned_paths,
                "forbid_patterns": list(defaults.slop.forbid_patterns),
            },
            "arch": {
                "enabled": defaults.arch.enabled,
                "src_root": defaults.arch.src_root,
                "rules": [],
            },
            "permissions": {
                "enabled": defaults.permissions.enabled,
                "default_deny": defaults.permissions.default_deny,
                "protected_paths": list(defaults.permissions.protected_paths),
                "allow_delete": defaults.permissions.allow_delete,
                "allow_symlinks": defaults.permissions.allow_symlinks,
            },
        },
    }

    global_path = global_config_path or (
        Path.home() / ".config" / "code-factory" / "factory.toml"
    )
    raw = _deep_merge(raw, _read_toml_if_exists(global_path))

    if repo_root is not None:
        repo_path = repo_root / ".code-factory" / "factory.toml"
        raw = _deep_merge(raw, _read_toml_if_exists(repo_path))

    f_sec = raw.get("factory", {})
    g_sec = raw.get("gates", {})
    s_sec = g_sec.get("slop", {})
    a_sec = g_sec.get("arch", {})
    p_sec = g_sec.get("permissions", {})

    gate_commands = [
        GateCommandConfig(
            name=str(item["name"]),
            cmd=str(item["cmd"]),
            timeout=int(item.get("timeout", 180)),
        )
        for item in g_sec.get("commands", [])
    ]

    arch_rules = [
        ArchRuleConfig(
            from_module=str(item["from"]),
            forbid=[str(x) for x in item.get("forbid", [])],
            forbid_calls=[str(x) for x in item.get("forbid_calls", [])],
        )
        for item in a_sec.get("rules", [])
    ]

    return FactoryConfig(
        mode=FactoryMode(str(f_sec.get("mode", FactoryMode.SOFTWARE.value))),
        max_workers=max(1, int(f_sec.get("max_workers", 3))),
        max_gate_retries=max(0, int(f_sec.get("max_gate_retries", 3))),
        max_review_rounds=max(0, int(f_sec.get("max_review_rounds", 2))),
        cross_model_review=bool(f_sec.get("cross_model_review", True)),
        task_timeout_seconds=max(10, int(f_sec.get("task_timeout_seconds", 600))),
        reviewer_timeout_seconds=max(10, int(f_sec.get("reviewer_timeout_seconds", 180))),
        worktree_root=str(f_sec.get("worktree_root", ".code-factory/worktrees")),
        routing={
            str(role): [str(name) for name in names]
            for role, names in raw.get("routing", {}).items()
        },
        adapters={
            str(name): dict(cfg)
            for name, cfg in raw.get("adapters", {}).items()
            if isinstance(cfg, dict)
        },
        gate_commands=gate_commands,
        slop=SlopGateConfig(
            enabled=bool(s_sec.get("enabled", True)),
            max_diff_lines=int(s_sec.get("max_diff_lines", 800)),
            count_deletions_in_diff_budget=bool(
                s_sec.get("count_deletions_in_diff_budget", False)
            ),
            max_comment_ratio=float(s_sec.get("max_comment_ratio", 0.35)),
            enforce_owned_paths=bool(s_sec.get("enforce_owned_paths", True)),
            forbid_patterns=[str(p) for p in s_sec.get("forbid_patterns", [])],
        ),
        arch=ArchGateConfig(
            enabled=bool(a_sec.get("enabled", False)),
            src_root=str(a_sec.get("src_root", "src")),
            rules=arch_rules,
        ),
        permissions=PermissionGateConfig(
            enabled=bool(p_sec.get("enabled", True)),
            default_deny=bool(p_sec.get("default_deny", True)),
            protected_paths=[str(p) for p in p_sec.get("protected_paths", [])],
            allow_delete=bool(p_sec.get("allow_delete", True)),
            allow_symlinks=bool(p_sec.get("allow_symlinks", False)),
        ),
    )


def detect_repo_gates(repo_root: Path) -> list[GateCommandConfig]:
    """Heuristically detect deterministic test/lint/typecheck commands for a target repo."""
    detected: list[GateCommandConfig] = []

    if (repo_root / "pyproject.toml").is_file() or (repo_root / "tests").is_dir():
        if (repo_root / "tests").is_dir():
            py_cmd = (
                "PYTHONPATH=src python3 -m unittest discover -s tests -v"
                if (repo_root / "src").is_dir()
                else "python3 -m unittest discover -s tests -v"
            )
            detected.append(GateCommandConfig(name="python-tests", cmd=py_cmd))

    if (repo_root / "package.json").is_file():
        detected.append(GateCommandConfig(name="npm-test", cmd="npm test"))

    if (repo_root / "Cargo.toml").is_file():
        detected.append(GateCommandConfig(name="cargo-test", cmd="cargo test"))

    if (repo_root / "go.mod").is_file():
        detected.append(GateCommandConfig(name="go-test", cmd="go test ./..."))

    return detected


def render_default_factory_toml(repo_root: Path) -> str:
    """Generate a clean starter `.code-factory/factory.toml` for a target repository."""
    detected = detect_repo_gates(repo_root)
    if detected:
        cmd_lines = [
            f'  {{ name = "{g.name}", cmd = "{g.cmd}", timeout = {g.timeout} }},'
            for g in detected
        ]
        commands_block = "commands = [\n" + "\n".join(cmd_lines) + "\n]"
    else:
        commands_block = "commands = []"

    return f"""[factory]
mode = "software"
max_workers = 3
max_gate_retries = 3
max_review_rounds = 2
task_timeout_seconds = 600
worktree_root = ".code-factory/worktrees"

[routing]
planner = ["codex", "claude", "agy"]
worker = ["codex", "claude", "agy"]
reviewer = ["claude", "agy", "codex"]

[adapters.codex]
type = "codex"
command = "codex"
sandbox = "workspace-write"
approve_for_me = true

[adapters.claude]
type = "claude"
command = "claude"
allowed_tools = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]

[adapters.agy]
type = "agy"
command = "agy"
mode = "accept-edits"
sandbox = true

[gates]
{commands_block}

[gates.slop]
enabled = true
max_diff_lines = 800
max_comment_ratio = 0.35
enforce_owned_paths = true
forbid_patterns = [
  "TODO\\\\(agent\\\\)",
  "FIXME\\\\(agent\\\\)",
  "NotImplementedError\\\\(\\"todo",
]

[gates.arch]
enabled = false
src_root = "src"

[gates.permissions]
enabled = true
default_deny = true
protected_paths = [
  ".code-factory/**",
  ".github/**",
]
allow_delete = true
allow_symlinks = false
"""
