"""Architect-guided parallel read-only full-repo code audit engine (`cf audit`)."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from code_factory.adapters.registry import AdapterRegistry
from code_factory.config import FactoryConfig
from code_factory.core.models import AgentInvocation, AgentRole, utc_now_iso

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".code-factory",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".eggs",
    }
)

_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".sql",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".sh",
        ".md",
    }
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

AUDIT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["repo_summary", "global_risks", "shards"],
    "properties": {
        "repo_summary": {"type": "string"},
        "global_risks": {"type": "array", "items": {"type": "string"}},
        "shards": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "paths", "focus"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "focus": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

SHARD_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["shard_summary", "findings"],
    "properties": {
        "shard_summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "severity",
                    "category",
                    "file",
                    "title",
                    "description",
                    "fix_hint",
                ],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix_hint": {"type": "string"},
                    "ratchet_rule_idea": {"type": "string"},
                },
            },
        },
    },
}

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["executive_summary", "systemic_risks", "recommended_gates"],
    "properties": {
        "executive_summary": {"type": "string"},
        "systemic_risks": {"type": "array", "items": {"type": "string"}},
        "recommended_gates": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class AuditShard:
    id: str
    title: str
    paths: list[str]
    focus: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditPlan:
    repo_summary: str
    global_risks: list[str]
    shards: list[AuditShard]


@dataclass(frozen=True)
class AuditFinding:
    shard_id: str
    reviewer: str
    severity: str
    category: str
    file: str
    line: int | None
    title: str
    description: str
    evidence: str = ""
    fix_hint: str = ""
    ratchet_rule_idea: str = ""


@dataclass(frozen=True)
class AuditResult:
    audit_id: str
    repo_root: str
    lead_adapter: str | None
    reviewer_adapter: str
    plan: AuditPlan
    findings: list[AuditFinding]
    shard_summaries: dict[str, str]
    shard_errors: dict[str, str]
    executive_summary: str
    systemic_risks: list[str]
    recommended_gates: list[str]
    report_path: str
    data_path: str
    created_at: str = field(default_factory=utc_now_iso)


def scan_repo_files(repo_root: Path, max_files: int = 400) -> list[tuple[str, int]]:
    """Return sorted list of (relative_path, line_count) for reviewable text files."""
    root = repo_root.resolve()
    entries: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        if len(entries) >= max_files:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                line_count = sum(1 for _ in fh)
        except OSError:
            continue
        entries.append((rel.as_posix(), line_count))
    return entries


def build_deterministic_shards(
    files: list[tuple[str, int]],
    *,
    max_files_per_shard: int = 12,
) -> AuditPlan:
    """Group repository files deterministically by top-level directory into review shards."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for rel_path, _ in files:
        parts = Path(rel_path).parts
        group = "/".join(parts[:2]) if len(parts) >= 3 else (parts[0] if len(parts) >= 2 else "root")
        grouped[group].append(rel_path)

    shards: list[AuditShard] = []
    for group in sorted(grouped):
        paths = grouped[group]
        for idx in range(0, len(paths), max_files_per_shard):
            chunk = paths[idx : idx + max_files_per_shard]
            suffix = f"-{idx // max_files_per_shard + 1}" if len(paths) > max_files_per_shard else ""
            safe_id = group.replace("/", "-").replace(".", "_") + suffix
            shards.append(
                AuditShard(
                    id=safe_id,
                    title=f"Review {group}{suffix}",
                    paths=chunk,
                    focus=[
                        "Correctness, edge cases, and silent failure modes",
                        "Architecture boundaries, dead code, and anti-slop hygiene",
                        "Test coverage gaps and falsifiable invariants",
                    ],
                )
            )
    if not shards:
        shards.append(
            AuditShard(
                id="repo",
                title="Full repository review",
                paths=[],
                focus=["General code quality and correctness"],
            )
        )
    return AuditPlan(
        repo_summary=f"Deterministic inventory of {len(files)} reviewable file(s).",
        global_risks=[
            "Correctness bugs and unhandled edge cases",
            "Architecture boundary drift and AI slop",
        ],
        shards=shards,
    )


class RepoAuditor:
    """Runs architect-guided (e.g. agy) + parallel worker (e.g. codex) read-only repo audits."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config: FactoryConfig,
        registry: AdapterRegistry,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
        self.registry = registry

    def run_audit(
        self,
        *,
        lead_adapter: str | None = "agy",
        reviewer_adapter: str = "codex",
        max_workers: int | None = None,
        focus: str | None = None,
        synthesize: bool = True,
        audit_id: str | None = None,
    ) -> AuditResult:
        aid = audit_id or f"audit-{uuid.uuid4().hex[:8]}"
        audit_dir = self.repo_root / ".code-factory" / "audits" / aid
        shards_dir = audit_dir / "shards"
        shards_dir.mkdir(parents=True, exist_ok=True)

        inventory = scan_repo_files(self.repo_root)
        valid_paths = {p for p, _ in inventory}

        lead = (
            self.registry.pick_adapter("planner", preferred=lead_adapter)
            if lead_adapter
            else None
        )
        reviewer = self.registry.pick_adapter("worker", preferred=reviewer_adapter)

        if lead is not None:
            plan = self._plan_with_lead(
                lead=lead,
                inventory=inventory,
                valid_paths=valid_paths,
                focus=focus,
                transcript_path=audit_dir / "lead_plan.log",
            )
        else:
            plan = build_deterministic_shards(inventory)

        (audit_dir / "audit_plan.json").write_text(
            json.dumps(asdict(plan), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        workers = max(1, min(max_workers or self.config.max_workers, len(plan.shards)))
        shard_summaries: dict[str, str] = {}
        shard_errors: dict[str, str] = {}
        raw_findings: list[AuditFinding] = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._review_shard,
                    reviewer=reviewer,
                    plan=plan,
                    shard=shard,
                    focus=focus,
                    shards_dir=shards_dir,
                ): shard
                for shard in plan.shards
            }
            for fut in as_completed(futures):
                shard = futures[fut]
                summary, findings, error = fut.result()
                if error:
                    shard_errors[shard.id] = error
                if summary:
                    shard_summaries[shard.id] = summary
                raw_findings.extend(findings)

        findings = self._dedupe_and_sort(raw_findings)

        exec_summary = (
            f"Audited {len(plan.shards)} shard(s) across {len(inventory)} file(s) "
            f"using reviewer `{reviewer.name}`"
            + (f" guided by `{lead.name}`." if lead else ".")
        )
        systemic_risks = list(plan.global_risks)
        recommended_gates: list[str] = []

        if lead is not None and synthesize and findings:
            synth = self._synthesize_with_lead(
                lead=lead,
                plan=plan,
                findings=findings,
                shard_errors=shard_errors,
                transcript_path=audit_dir / "lead_synthesis.log",
            )
            if synth is not None:
                exec_summary = str(synth.get("executive_summary", exec_summary))
                systemic_risks = [str(x) for x in synth.get("systemic_risks", systemic_risks)]
                recommended_gates = [str(x) for x in synth.get("recommended_gates", [])]

        if not recommended_gates:
            seen_ideas: set[str] = set()
            for f in findings:
                idea = f.ratchet_rule_idea.strip()
                if idea and idea not in seen_ideas:
                    seen_ideas.add(idea)
                    recommended_gates.append(idea)
                if len(recommended_gates) >= 8:
                    break

        report_path = audit_dir / "audit_report.md"
        data_path = audit_dir / "audit_data.json"

        result = AuditResult(
            audit_id=aid,
            repo_root=str(self.repo_root),
            lead_adapter=lead.name if lead else None,
            reviewer_adapter=reviewer.name,
            plan=plan,
            findings=findings,
            shard_summaries=shard_summaries,
            shard_errors=shard_errors,
            executive_summary=exec_summary,
            systemic_risks=systemic_risks,
            recommended_gates=recommended_gates,
            report_path=str(report_path),
            data_path=str(data_path),
        )

        data_path.write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        report_path.write_text(self._render_report(result), encoding="utf-8")
        return result

    def _plan_with_lead(
        self,
        *,
        lead: Any,
        inventory: list[tuple[str, int]],
        valid_paths: set[str],
        focus: str | None,
        transcript_path: Path,
    ) -> AuditPlan:
        inv_lines = [f"- {path} ({lines} lines)" for path, lines in inventory[:250]]
        prompt = "\n".join(
            [
                "# Code Factory Audit Director",
                f"Repository: {self.repo_root}",
                f"Extra focus requested by user: {focus or 'Full-repo deep code review'}",
                "",
                "## Repository File Inventory",
                *inv_lines,
                "",
                "## Your Job (Read-Only Planning)",
                "1. Inspect the structure and key entry points briefly if needed.",
                "2. Identify the highest-leverage repository-wide risks (e.g., correctness, data integrity, lookahead bias / numerical stability for quant code, concurrency, architecture boundaries, missing tests).",
                "3. Partition ALL important source & test files into 3 to 8 cohesive, disjoint review shards so parallel Codex reviewers can audit every shard deeply.",
                "4. For each shard, list exact relative file paths from the inventory above and 2-5 razor-sharp, shard-specific focus questions.",
                "",
                "Return ONLY valid JSON matching the AuditPlan schema.",
            ]
        )
        res = lead.run(
            AgentInvocation(
                prompt=prompt,
                cwd=str(self.repo_root),
                role=AgentRole.PLANNER,
                transcript_path=str(transcript_path),
                timeout_seconds=self.config.task_timeout_seconds,
                read_only=True,
                json_schema=AUDIT_PLAN_SCHEMA,
            )
        )
        parsed = res.parsed_json
        if not res.ok or not parsed or not isinstance(parsed.get("shards"), list):
            return build_deterministic_shards(inventory)

        shards: list[AuditShard] = []
        covered: set[str] = set()
        for idx, raw in enumerate(parsed["shards"], start=1):
            if not isinstance(raw, dict):
                continue
            raw_paths = [str(p).lstrip("./") for p in raw.get("paths", [])]
            filtered = [p for p in raw_paths if p in valid_paths] if valid_paths else raw_paths
            if not filtered and valid_paths:
                continue
            covered.update(filtered)
            shards.append(
                AuditShard(
                    id=str(raw.get("id", f"shard-{idx}")).replace("/", "-"),
                    title=str(raw.get("title", f"Shard {idx}")),
                    paths=filtered,
                    focus=[str(x) for x in raw.get("focus", [])],
                )
            )

        if not shards:
            return build_deterministic_shards(inventory)

        uncovered = [p for p, _ in inventory if p not in covered and p.endswith(".py")]
        if uncovered:
            shards.append(
                AuditShard(
                    id="remaining-py",
                    title="Remaining Python modules",
                    paths=uncovered[:20],
                    focus=["Correctness, edge cases, and consistency with the rest of the repo"],
                )
            )

        return AuditPlan(
            repo_summary=str(parsed.get("repo_summary", "")),
            global_risks=[str(x) for x in parsed.get("global_risks", [])],
            shards=shards,
        )

    def _review_shard(
        self,
        *,
        reviewer: Any,
        plan: AuditPlan,
        shard: AuditShard,
        focus: str | None,
        shards_dir: Path,
    ) -> tuple[str, list[AuditFinding], str | None]:
        prompt = "\n".join(
            [
                f"# Deep Adversarial Code Review — Shard `{shard.id}`: {shard.title}",
                f"Repository: {self.repo_root}",
                "",
                "## Architect Context",
                plan.repo_summary,
                "",
                "## Repository-Wide Risk Checklist",
                *(f"- {r}" for r in plan.global_risks),
                "",
                "## Files Assigned to This Shard",
                *(f"- {p}" for p in shard.paths),
                "",
                "## Shard-Specific Focus Questions from Lead Architect",
                *(f"- {q}" for q in shard.focus),
                *(["", f"## Extra User Focus: {focus}"] if focus else []),
                "",
                "## Strict Review Rules",
                "- You are in READ-ONLY mode. Do NOT modify any files.",
                "- Read the assigned files thoroughly and cite concrete file paths + line numbers + evidence.",
                "- Focus on REAL bugs, subtle edge cases, silent data/numerical errors, architectural violations, and missing falsifiable tests.",
                "- Do NOT emit style nitpicks or speculative praise.",
                "- For recurring bug classes, suggest a `ratchet_rule_idea` (lint/slop/arch/test check).",
                "- Human-readable reporting: On first mention of any repo-internal file, class, or function in `title`, `description`, or `fix_hint`, add a brief parenthetical role gloss (3-10 words) so a human reader can follow without memorizing internal symbols; skip stdlib/common terms and do not repeat glosses.",
                "",
                "Return ONLY valid JSON matching the ShardReview schema.",
            ]
        )
        transcript = shards_dir / f"{shard.id}.log"
        res = reviewer.run(
            AgentInvocation(
                prompt=prompt,
                cwd=str(self.repo_root),
                role=AgentRole.REVIEWER,
                transcript_path=str(transcript),
                timeout_seconds=self.config.task_timeout_seconds,
                read_only=True,
                json_schema=SHARD_REVIEW_SCHEMA,
            )
        )
        parsed = res.parsed_json
        if not res.ok or not parsed or not isinstance(parsed.get("findings"), list):
            err = f"Reviewer `{reviewer.name}` failed on shard `{shard.id}` (exit={res.exit_code}). See {transcript}"
            return "", [], err

        (shards_dir / f"{shard.id}.json").write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        findings: list[AuditFinding] = []
        for item in parsed.get("findings", []):
            if not isinstance(item, dict):
                continue
            sev = str(item.get("severity", "medium")).lower()
            if sev not in _SEVERITY_ORDER:
                sev = "medium"
            findings.append(
                AuditFinding(
                    shard_id=shard.id,
                    reviewer=reviewer.name,
                    severity=sev,
                    category=str(item.get("category", "correctness")),
                    file=str(item.get("file", "")),
                    line=item.get("line") if isinstance(item.get("line"), int) else None,
                    title=str(item.get("title", "Untitled finding")),
                    description=str(item.get("description", "")),
                    evidence=str(item.get("evidence", "")),
                    fix_hint=str(item.get("fix_hint", "")),
                    ratchet_rule_idea=str(item.get("ratchet_rule_idea", "")),
                )
            )
        return str(parsed.get("shard_summary", "")), findings, None

    def _synthesize_with_lead(
        self,
        *,
        lead: Any,
        plan: AuditPlan,
        findings: list[AuditFinding],
        shard_errors: dict[str, str],
        transcript_path: Path,
    ) -> dict[str, Any] | None:
        compact = [
            {
                "severity": f.severity,
                "category": f.category,
                "file": f.file,
                "line": f.line,
                "title": f.title,
                "description": f.description[:300],
                "ratchet_rule_idea": f.ratchet_rule_idea[:200],
            }
            for f in findings[:60]
        ]
        prompt = "\n".join(
            [
                "# Code Factory Audit Synthesis",
                f"Repository: {self.repo_root}",
                f"Architect summary: {plan.repo_summary}",
                f"Shard errors: {json.dumps(shard_errors)}",
                "",
                "Below are the structured findings collected from parallel shard reviewers.",
                "Synthesize:",
                "1. `executive_summary`: concise assessment of overall repo health and top priorities.",
                "2. `systemic_risks`: 3-6 cross-cutting patterns seen across modules.",
                "3. `recommended_gates`: 3-7 concrete deterministic checks (slop patterns, AST boundaries, invariant tests) to add to `.code-factory/factory.toml`.",
                "4. Human-readable reporting: On first mention of any repo-internal file, class, or function in `executive_summary` or `systemic_risks`, add a brief parenthetical role gloss (3-10 words); skip stdlib/common terms and do not repeat glosses.",
                "",
                "## Findings JSON",
                json.dumps(compact, indent=2, ensure_ascii=False),
                "",
                "Return ONLY valid JSON matching the Synthesis schema.",
            ]
        )
        res = lead.run(
            AgentInvocation(
                prompt=prompt,
                cwd=str(self.repo_root),
                role=AgentRole.REVIEWER,
                transcript_path=str(transcript_path),
                timeout_seconds=self.config.task_timeout_seconds,
                read_only=True,
                json_schema=SYNTHESIS_SCHEMA,
            )
        )
        if res.ok and res.parsed_json:
            return res.parsed_json
        return None

    @staticmethod
    def _dedupe_and_sort(findings: list[AuditFinding]) -> list[AuditFinding]:
        seen: set[tuple[str, int | None, str]] = set()
        unique: list[AuditFinding] = []
        for f in findings:
            key = (f.file.strip(), f.line, f.title.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(f)
        unique.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.file, f.line or 0))
        return unique

    @staticmethod
    def _render_report(result: AuditResult) -> str:
        counts = {s: 0 for s in ("critical", "high", "medium", "low")}
        for f in result.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1

        lines = [
            f"# Code Factory Full-Repo Audit Report: `{result.audit_id}`",
            "",
            f"- **Repository**: `{result.repo_root}`",
            f"- **Audit Lead (Architect)**: `{result.lead_adapter or 'deterministic'}`",
            f"- **Parallel Reviewer CLI**: `{result.reviewer_adapter}`",
            f"- **Shards Audited**: {len(result.plan.shards)} (errors: {len(result.shard_errors)})",
            f"- **Findings**: {len(result.findings)} total "
            f"(critical={counts['critical']}, high={counts['high']}, "
            f"medium={counts['medium']}, low={counts['low']})",
            "",
            "## Executive Summary",
            "",
            result.executive_summary,
            "",
        ]

        if result.systemic_risks:
            lines.extend(["## Systemic Risks", ""])
            for risk in result.systemic_risks:
                lines.append(f"- {risk}")
            lines.append("")

        if result.recommended_gates:
            lines.extend(["## Recommended Deterministic Gates (Inverse Trust Pyramid)", ""])
            for gate in result.recommended_gates:
                lines.append(f"- {gate}")
            lines.append("")

        if result.shard_errors:
            lines.extend(["## Shard Errors", ""])
            for sid, err in result.shard_errors.items():
                lines.append(f"- `{sid}`: {err}")
            lines.append("")

        lines.extend(["## Detailed Findings", ""])
        if not result.findings:
            lines.append("No findings reported.")
        else:
            for sev in ("critical", "high", "medium", "low"):
                bucket = [f for f in result.findings if f.severity == sev]
                if not bucket:
                    continue
                lines.extend([f"### {sev.upper()} ({len(bucket)})", ""])
                for idx, f in enumerate(bucket, start=1):
                    loc = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
                    lines.append(f"#### {idx}. [{f.category}] {f.title} ({loc})")
                    lines.append(f"- **Shard**: `{f.shard_id}` (`{f.reviewer}`)")
                    lines.append(f"- **Details**: {f.description}")
                    if f.evidence:
                        lines.append(f"- **Evidence**: `{f.evidence}`")
                    lines.append(f"- **Fix**: {f.fix_hint}")
                    if f.ratchet_rule_idea:
                        lines.append(f"- **Ratchet / Gate Idea**: {f.ratchet_rule_idea}")
                    lines.append("")

        return "\n".join(lines).strip() + "\n"
