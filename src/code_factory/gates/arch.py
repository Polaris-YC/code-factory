"""Zero-dependency Python AST architecture import & capability-boundary gate."""

from __future__ import annotations

import ast
from pathlib import Path

from code_factory.config import ArchGateConfig
from code_factory.core.models import GateCheckResult


def _file_to_module(py_file: Path, src_root: Path) -> str:
    rel = py_file.relative_to(src_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _resolve_relative_import(current_module: str, level: int, target: str | None) -> str:
    parts = current_module.split(".")
    base = parts[:-level] if level <= len(parts) else []
    if target:
        base.extend(target.split("."))
    return ".".join(p for p in base if p)


def _extract_dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _extract_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _resolve_callee(dotted: str, aliases: dict[str, str]) -> str:
    parts = dotted.split(".")
    root = parts[0]
    if root in aliases:
        mapped = aliases[root]
        return ".".join([mapped, *parts[1:]]) if len(parts) > 1 else mapped
    return dotted


def _matches_call_pattern(callee: str, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    normalized_callee = callee[len("builtins.") :] if callee.startswith("builtins.") else callee
    if pat.endswith(".*"):
        prefix = pat[:-2]
        return _matches_prefix(normalized_callee, prefix)
    return _matches_prefix(normalized_callee, pat)


def run_arch_gate(repo_root: Path, config: ArchGateConfig) -> GateCheckResult:
    """Enforce directional Python import and capability-call boundaries declared in `[gates.arch]`."""
    if not config.enabled or not config.rules:
        return GateCheckResult(
            name="arch-gate",
            passed=True,
            exit_code=0,
            summary="Architecture boundary gate disabled or no rules configured.",
        )

    src_dir = (repo_root / config.src_root).resolve()
    if not src_dir.is_dir():
        return GateCheckResult(
            name="arch-gate",
            passed=False,
            exit_code=1,
            summary=f"Architecture gate src_root not found: {src_dir}",
        )

    violations: list[str] = []
    scanned = 0

    for py_file in sorted(src_dir.rglob("*.py")):
        scanned += 1
        module_name = _file_to_module(py_file, src_dir)
        active_rules = [
            rule for rule in config.rules if _matches_prefix(module_name, rule.from_module)
        ]
        if not active_rules:
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as exc:
            violations.append(f"{py_file.relative_to(repo_root)}:{exc.lineno}: SyntaxError: {exc.msg}")
            continue

        imports: list[tuple[int, str]] = []
        aliases: dict[str, str] = {}
        calls: list[tuple[int, str]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((node.lineno, alias.name))
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        aliases[top] = top
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    resolved = _resolve_relative_import(module_name, node.level, node.module)
                else:
                    resolved = node.module or ""
                if resolved:
                    imports.append((node.lineno, resolved))
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        local_name = alias.asname or alias.name
                        aliases[local_name] = f"{resolved}.{alias.name}"

        has_call_rules = any(rule.forbid_calls for rule in active_rules)
        if has_call_rules:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    dotted = _extract_dotted_name(node.func)
                    if dotted:
                        calls.append((node.lineno, _resolve_callee(dotted, aliases)))

        rel_path = py_file.relative_to(repo_root)
        for lineno, imported in imports:
            for rule in active_rules:
                for forbidden in rule.forbid:
                    if _matches_prefix(imported, forbidden):
                        violations.append(
                            f"{rel_path}:{lineno}: module '{module_name}' imports "
                            f"forbidden dependency '{imported}' (rule: {rule.from_module} !-> {forbidden})"
                        )

        for lineno, callee in calls:
            for rule in active_rules:
                for forbidden_call in rule.forbid_calls:
                    if _matches_call_pattern(callee, forbidden_call):
                        violations.append(
                            f"{rel_path}:{lineno}: module '{module_name}' calls "
                            f"forbidden capability '{callee}' (rule: {rule.from_module} !call {forbidden_call})"
                        )

    if violations:
        return GateCheckResult(
            name="arch-gate",
            passed=False,
            exit_code=1,
            summary=f"Architecture gate failed with {len(violations)} boundary violation(s).",
            excerpt="\n".join(f"- {v}" for v in violations[:25]),
        )

    return GateCheckResult(
        name="arch-gate",
        passed=True,
        exit_code=0,
        summary=f"Architecture gate passed ({scanned} Python file(s), {len(config.rules)} rule(s)).",
    )
