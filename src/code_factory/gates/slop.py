"""Deterministic anti-slop diff hygiene gate."""

from __future__ import annotations

import re
from fnmatch import fnmatch

from code_factory.config import SlopGateConfig
from code_factory.core.models import GateCheckResult

_NON_CODE_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".json", ".toml", ".yaml", ".yml")


def _path_matches_owned(path: str, owned_paths: list[str]) -> bool:
    norm = path.strip().lstrip("./")
    for raw_pattern in owned_paths:
        pat = raw_pattern.strip().lstrip("./")
        if not pat:
            continue
        if pat.endswith("/") and norm.startswith(pat):
            return True
        if pat.endswith("/**") and norm.startswith(pat[:-2]):
            return True
        if fnmatch(norm, pat):
            return True
    return False


def _is_comment_line(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
        or stripped.startswith("--")
    )


def run_slop_gate(
    *,
    changed_files: list[str],
    diff_text: str,
    config: SlopGateConfig,
    owned_paths: list[str] | None = None,
    max_diff_lines_override: int | None = None,
) -> GateCheckResult:
    """Validate diff scope, size, comment ratio, and forbidden slop patterns."""
    if not config.enabled:
        return GateCheckResult(
            name="slop-gate",
            passed=True,
            exit_code=0,
            summary="Slop gate disabled by config.",
        )

    violations: list[str] = []

    if config.enforce_owned_paths and owned_paths:
        out_of_scope = [
            f for f in changed_files if not _path_matches_owned(f, owned_paths)
        ]
        if out_of_scope:
            violations.append(
                f"Out-of-scope file modifications (allowed={owned_paths}): "
                + ", ".join(out_of_scope)
            )

    added_lines: list[tuple[str, str]] = []
    removed_count = 0
    current_file = "<unknown>"

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith('+++ "b/'):
            current_file = line[7:].strip().removesuffix('"')
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added_lines.append((current_file, line[1:]))
        elif line.startswith("-"):
            removed_count += 1

    measured_lines = (
        len(added_lines) + removed_count
        if config.count_deletions_in_diff_budget
        else len(added_lines)
    )
    budget = (
        max_diff_lines_override
        if max_diff_lines_override is not None
        else config.max_diff_lines
    )
    if budget > 0 and measured_lines > budget:
        label = "Diff churn" if config.count_deletions_in_diff_budget else "Added lines"
        violations.append(
            f"{label} ({measured_lines} lines) exceeds budget ({budget} lines)."
        )

    non_empty_code_lines = [
        content
        for fname, content in added_lines
        if content.strip() and not fname.lower().endswith(_NON_CODE_SUFFIXES)
    ]
    if len(non_empty_code_lines) >= 20:
        comment_count = sum(1 for content in non_empty_code_lines if _is_comment_line(content))
        ratio = comment_count / len(non_empty_code_lines)
        if ratio > config.max_comment_ratio:
            violations.append(
                f"Comment-to-code ratio on added lines ({ratio:.2f}) exceeds "
                f"max_comment_ratio ({config.max_comment_ratio:.2f})."
            )

    compiled = [
        (pattern, re.compile(pattern)) for pattern in config.forbid_patterns
    ]
    for fname, content in added_lines:
        for pattern, regex in compiled:
            if regex.search(content):
                violations.append(
                    f"Forbidden pattern /{pattern}/ matched in {fname}: {content.strip()[:100]}"
                )

    if violations:
        return GateCheckResult(
            name="slop-gate",
            passed=False,
            exit_code=1,
            summary=f"Slop gate failed with {len(violations)} violation(s).",
            excerpt="\n".join(f"- {v}" for v in violations[:25]),
        )

    return GateCheckResult(
        name="slop-gate",
        passed=True,
        exit_code=0,
        summary=(
            f"Slop gate passed ({len(changed_files)} file(s), "
            f"+{len(added_lines)}/-{removed_count} lines)."
        ),
    )
