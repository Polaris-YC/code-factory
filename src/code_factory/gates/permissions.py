"""Strict code permission boundary gate and lightweight static policy verifier."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from code_factory.config import PermissionGateConfig
from code_factory.core.models import GateCheckResult, TaskSpec


class PermissionPolicyError(ValueError):
    """Raised when a path or permission pattern violates boundary invariants."""


@dataclass(frozen=True)
class DiffFileOp:
    path: str
    old_path: str | None = None
    deleted: bool = False
    is_symlink: bool = False


def normalize_rel_path(raw: str) -> str:
    """Validate and normalize a repository-relative path, rejecting traversal or absolutes."""
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned:
        raise PermissionPolicyError("Empty path is not allowed.")
    if "\x00" in cleaned:
        raise PermissionPolicyError(f"NUL byte in path: {raw!r}")
    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha()):
        raise PermissionPolicyError(f"Absolute path is not allowed: {raw!r}")

    parts: list[str] = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PermissionPolicyError(f"Path traversal ('..') is not allowed: {raw!r}")
        parts.append(part)

    if not parts:
        raise PermissionPolicyError(f"Path resolves to repository root: {raw!r}")
    return "/".join(parts)


def normalize_pattern(raw: str) -> str:
    """Validate and normalize a relative glob/prefix permission pattern."""
    cleaned = raw.strip().replace("\\", "/")
    trailing_slash = cleaned.endswith("/")
    norm = normalize_rel_path(cleaned.rstrip("/") if trailing_slash else cleaned)
    return f"{norm}/" if trailing_slash else norm


def pattern_matches_path(norm_path: str, norm_pattern: str) -> bool:
    """Return True if a normalized relative path matches a normalized permission pattern."""
    if norm_pattern in ("**", "*"):
        return True
    if norm_pattern.endswith("/"):
        return norm_path.startswith(norm_pattern)
    if norm_pattern.endswith("/**"):
        prefix = norm_pattern[:-3]
        return norm_path == prefix or norm_path.startswith(prefix + "/")
    return fnmatch(norm_path, norm_pattern)


def _static_prefix(norm_pattern: str) -> str:
    for idx, ch in enumerate(norm_pattern):
        if ch in "*?[":
            return norm_pattern[:idx].rstrip("/")
    return norm_pattern.rstrip("/")


def patterns_overlap(patterns_a: list[str], patterns_b: list[str]) -> bool:
    """Conservative static overlap check between two pattern sets."""
    if not patterns_a or not patterns_b:
        return True
    for raw_a in patterns_a:
        na = normalize_pattern(raw_a)
        pa = _static_prefix(na)
        for raw_b in patterns_b:
            nb = normalize_pattern(raw_b)
            pb = _static_prefix(nb)
            if (
                na == nb
                or pattern_matches_path(na.rstrip("/"), nb)
                or pattern_matches_path(nb.rstrip("/"), na)
            ):
                return True
            if not pa or not pb:
                return True
            if pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                return True
    return False


def _unquote_git_path(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text


def extract_diff_operations(diff_text: str) -> list[DiffFileOp]:
    """Extract file paths, renames, deletions, and symlink modes from unified git diff."""
    ops: list[DiffFileOp] = []
    cur_old: str | None = None
    cur_new: str | None = None
    deleted = False
    is_symlink = False

    def flush() -> None:
        nonlocal cur_old, cur_new, deleted, is_symlink
        if cur_new is not None or cur_old is not None:
            target = cur_new or cur_old or ""
            old_val = cur_old if (cur_old and cur_old != target) else None
            ops.append(
                DiffFileOp(
                    path=target,
                    old_path=old_val,
                    deleted=deleted,
                    is_symlink=is_symlink,
                )
            )
        cur_old = None
        cur_new = None
        deleted = False
        is_symlink = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            body = line[len("diff --git ") :].strip()
            parts = body.split(" b/", 1)
            if len(parts) == 2:
                left = _unquote_git_path(parts[0])
                right = _unquote_git_path(parts[1])
                if left.startswith("a/"):
                    left = left[2:]
                cur_old = left
                cur_new = right
        elif line.startswith("rename from "):
            cur_old = _unquote_git_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            cur_new = _unquote_git_path(line[len("rename to ") :])
        elif line.startswith("deleted file mode "):
            deleted = True
        elif line.startswith("new file mode 120000") or line.startswith("new mode 120000"):
            is_symlink = True

    flush()
    return ops


def verify_permission_policy(
    tasks: list[TaskSpec],
    config: PermissionGateConfig,
) -> list[str]:
    """Statically verify DAG permission invariants before any worker runs.

    Proves:
    1. All protected_paths and task owned_paths are valid relative patterns (no traversal/absolutes).
    2. Under default_deny, every task declares explicit non-empty owned_paths.
    3. Non-escalation: no task's owned_paths may overlap global protected_paths.
    """
    if not config.enabled:
        return []

    violations: list[str] = []
    norm_protected: list[str] = []
    for pat in config.protected_paths:
        try:
            norm_protected.append(normalize_pattern(pat))
        except PermissionPolicyError as exc:
            violations.append(f"Invalid protected_paths pattern {pat!r}: {exc}")

    for task in tasks:
        if config.default_deny and not task.owned_paths:
            violations.append(
                f"Task '{task.id}' has empty owned_paths (forbidden under default_deny)."
            )
            continue

        norm_owned: list[str] = []
        for pat in task.owned_paths:
            try:
                norm_owned.append(normalize_pattern(pat))
            except PermissionPolicyError as exc:
                violations.append(
                    f"Task '{task.id}' has invalid owned_paths pattern {pat!r}: {exc}"
                )

        if norm_protected and norm_owned:
            for owned in norm_owned:
                for prot in norm_protected:
                    if patterns_overlap([owned], [prot]):
                        violations.append(
                            f"Task '{task.id}' owned_paths pattern {owned!r} overlaps "
                            f"protected_paths pattern {prot!r} (non-escalation invariant)."
                        )

    return violations


def run_permission_gate(
    *,
    cwd: Path,
    changed_files: list[str],
    diff_text: str,
    config: PermissionGateConfig,
    owned_paths: list[str] | None = None,
) -> GateCheckResult:
    """Enforce strict file permission boundaries on changed files, renames, deletes, and symlinks."""
    if not config.enabled:
        return GateCheckResult(
            name="permission-gate",
            passed=True,
            exit_code=0,
            summary="Permission boundary gate disabled by config.",
        )

    violations: list[str] = []

    norm_protected: list[str] = []
    for pat in config.protected_paths:
        try:
            norm_protected.append(normalize_pattern(pat))
        except PermissionPolicyError as exc:
            violations.append(f"Invalid protected_paths pattern {pat!r}: {exc}")

    norm_owned: list[str] | None = None
    if owned_paths is not None:
        norm_owned = []
        for pat in owned_paths:
            try:
                norm_owned.append(normalize_pattern(pat))
            except PermissionPolicyError as exc:
                violations.append(f"Invalid owned_paths pattern {pat!r}: {exc}")

    diff_ops = extract_diff_operations(diff_text)
    raw_touched: list[str] = list(changed_files)
    for op in diff_ops:
        raw_touched.append(op.path)
        if op.old_path:
            raw_touched.append(op.old_path)

    norm_touched: list[str] = []
    seen: set[str] = set()
    for raw in raw_touched:
        try:
            norm = normalize_rel_path(raw)
        except PermissionPolicyError as exc:
            violations.append(f"Disallowed file path {raw!r}: {exc}")
            continue
        if norm not in seen:
            seen.add(norm)
            norm_touched.append(norm)

    # 1. Global Deny / Protected paths (always wins over allowlists).
    for path in norm_touched:
        for prot in norm_protected:
            if pattern_matches_path(path, prot):
                violations.append(
                    f"Protected path modification denied: '{path}' matches protected_paths '{prot}'."
                )
                break

    # 2. Task-scoped Default-Deny over owned_paths.
    if norm_owned is not None and config.default_deny:
        if not norm_owned and norm_touched:
            violations.append(
                "Default-deny violation: task has empty owned_paths but modified: "
                + ", ".join(norm_touched)
            )
        else:
            for path in norm_touched:
                if not any(pattern_matches_path(path, pat) for pat in norm_owned):
                    violations.append(
                        f"Permission denied (outside owned_paths={norm_owned}): '{path}'."
                    )

    # 3. Diff operation restrictions (delete / symlink mode).
    for op in diff_ops:
        if op.deleted and not config.allow_delete:
            violations.append(f"File deletion denied (allow_delete=false): '{op.path}'.")
        if op.is_symlink and not config.allow_symlinks:
            violations.append(f"Symlink creation denied (allow_symlinks=false): '{op.path}'.")

    # 4. On-disk symlink and repo-root escape check.
    repo_root = cwd.resolve()
    for path in norm_touched:
        candidate = cwd / path
        if candidate.is_symlink():
            if not config.allow_symlinks:
                violations.append(f"Symlink denied on disk (allow_symlinks=false): '{path}'.")
            else:
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(repo_root)
                except ValueError:
                    violations.append(
                        f"Symlink escape denied: '{path}' resolves outside repository root."
                    )

    if violations:
        return GateCheckResult(
            name="permission-gate",
            passed=False,
            exit_code=1,
            summary=f"Permission gate failed with {len(violations)} violation(s).",
            excerpt="\n".join(f"- {v}" for v in violations[:25]),
        )

    return GateCheckResult(
        name="permission-gate",
        passed=True,
        exit_code=0,
        summary=f"Permission gate passed ({len(norm_touched)} touched path(s) verified).",
    )
