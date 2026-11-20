"""Command-line interface (`cf`) for Code Factory."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from code_factory.adapters.registry import AdapterRegistry
from code_factory.config import load_config, render_default_factory_toml
from code_factory.core.auditor import RepoAuditor
from code_factory.core.installer import install_bundle
from code_factory.core.models import FactoryMode, TaskDAG
from code_factory.core.orchestrator import FactoryOrchestrator
from code_factory.core.ratchet import RatchetInbox
from code_factory.core.state import RunStore
from code_factory.core.worktree import GitWorktreeManager
from code_factory.gates.runner import GateRunner


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_config(repo)
    registry = AdapterRegistry.from_config(config.adapters, config.routing)

    print("Code Factory Cluster Doctor")
    print(f"Repository: {repo}")
    print("")
    print("Core Tools:")
    for binary in ("git", "gh", "python3"):
        found = shutil.which(binary)
        status = f"OK ({found})" if found else "MISSING"
        print(f"  - {binary:<10} {status}")

    print("")
    print("Configured CLI Adapters:")
    available_count = 0
    for cap in registry.probe_all(refresh=True):
        if cap.available:
            available_count += 1
            ver = f" [{cap.version}]" if cap.version else ""
            print(f"  - {cap.name:<12} ({cap.adapter_type:<7}) OK   -> {cap.resolved_path}{ver}")
        else:
            print(f"  - {cap.name:<12} ({cap.adapter_type:<7}) MISS -> `{cap.binary}` not in PATH")

    print("")
    if available_count >= 2:
        print(f"Cluster Status: READY (cross-model adversarial review enabled: {available_count} CLIs).")
    elif available_count == 1:
        print("Cluster Status: READY (single-CLI mode; reviewer will use fresh adversarial context).")
    else:
        print("Cluster Status: NO LIVE CLI ADAPTERS FOUND (configure adapters in factory.toml).")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    target = repo / ".code-factory" / "factory.toml"
    if target.is_file() and not args.force:
        print(f"Already exists: {target} (pass --force to overwrite)")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_default_factory_toml(repo), encoding="utf-8")
    print(f"Initialized {target}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_config(repo)
    runner = GateRunner(config)

    changed: list[str] = []
    diff_text = ""
    if (repo / ".git").exists():
        wt = GitWorktreeManager(repo, config.worktree_root)
        base = args.base or "HEAD"
        if wt.ensure_branch_exists(base):
            changed = wt.changed_files(repo, base)
            diff_text = wt.unified_diff(repo, base)

    report = runner.run_all(
        cwd=repo,
        changed_files=changed,
        diff_text=diff_text,
        owned_paths=None,
        done_checks=args.check or [],
    )
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"[{mark}] {check.name}: {check.summary}")
        if not check.passed and check.excerpt:
            print(check.excerpt)
            print("")
    return 0 if report.passed else 1


def _cmd_plan(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_config(repo)
    registry = AdapterRegistry.from_config(config.adapters, config.routing)
    orch = FactoryOrchestrator(repo_root=repo, config=config, registry=registry)
    mode = FactoryMode.DARK if args.dark else config.mode
    dag = orch.synthesize_dag(
        goal=args.goal,
        base_branch=args.base,
        mode=mode,
        planner_adapter=args.adapter,
    )
    payload = json.dumps(dag.to_dict(), indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote validated Task DAG to {out_path}")
    else:
        print(payload)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_config(repo)
    dag_data = json.loads(Path(args.dag).read_text(encoding="utf-8"))
    if args.dark:
        dag_data["mode"] = FactoryMode.DARK.value
    dag = TaskDAG.from_dict(dag_data)

    registry = AdapterRegistry.from_config(config.adapters, config.routing)
    orch = FactoryOrchestrator(repo_root=repo, config=config, registry=registry)
    state = orch.run_dag(
        dag,
        create_pr=args.pr,
        keep_worktrees=args.keep_worktrees,
        cross_model_review=False if args.no_cross_model else None,
    )

    print(f"Run `{state.run_id}` finished: integrated={state.integrated}, merged={state.merged_to_base}")
    for tid, ts in state.tasks.items():
        print(f"  - {tid}: {ts.status.value} (worker={ts.worker_adapter}, reviewer={ts.reviewer_adapter})")
    return 0 if state.integrated else 2


def _cmd_resume(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = load_config(repo)
    registry = AdapterRegistry.from_config(config.adapters, config.routing)
    orch = FactoryOrchestrator(repo_root=repo, config=config, registry=registry)
    state = orch.resume_run(
        args.run_id,
        create_pr=args.pr,
        keep_worktrees=args.keep_worktrees,
        cross_model_review=False if args.no_cross_model else None,
    )
    print(f"Resumed `{state.run_id}`: integrated={state.integrated}, merged={state.merged_to_base}")
    for tid, ts in state.tasks.items():
        print(f"  - {tid}: {ts.status.value} (worker={ts.worker_adapter}, reviewer={ts.reviewer_adapter})")
    return 0 if state.integrated else 2


def _cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.run_id:
        store = RunStore(repo, args.run_id)
        if not store.state_path.is_file():
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        print(store.state_path.read_text(encoding="utf-8"))
        return 0

    runs = RunStore.list_runs(repo)
    if not runs:
        print("No Code Factory runs recorded in this repository.")
        return 0
    for item in runs[:10]:
        print(
            f"- {item['run_id']} | mode={item['mode']} | "
            f"integrated={item['integrated']} | goal={item['goal'][:60]}"
        )
    return 0


def _cmd_ratchet(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    inbox = RatchetInbox(repo)
    print(inbox.summarize())
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    config = load_config(repo)
    registry = AdapterRegistry.from_config(config.adapters, config.routing)
    auditor = RepoAuditor(repo_root=repo, config=config, registry=registry)
    result = auditor.run_audit(
        lead_adapter=None if args.no_lead else args.lead,
        reviewer_adapter=args.reviewer,
        max_workers=args.workers,
        focus=args.focus,
        synthesize=not args.no_synth,
        audit_id=args.audit_id,
    )
    print(
        f"Audit `{result.audit_id}` complete: "
        f"{len(result.findings)} finding(s) across {len(result.plan.shards)} shard(s) "
        f"(lead={result.lead_adapter or 'none'}, reviewer={result.reviewer_adapter}, "
        f"errors={len(result.shard_errors)})."
    )
    print(f"Report: {result.report_path}")
    print(f"Data:   {result.data_path}")
    return 0 if not result.shard_errors else 2


def _cmd_install(args: argparse.Namespace) -> int:
    actions = install_bundle(
        package_root=_package_root(),
        target=args.target,
        scope=args.scope,
        workspace=Path(args.workspace).resolve() if args.workspace else None,
        copy=args.copy,
        dry_run=args.dry_run,
    )
    prefix = "[DRY-RUN] " if args.dry_run else ""
    for act in actions:
        print(f"{prefix}[{act.target}] {act.mode}: {act.source} -> {act.destination}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cf",
        description="Code Factory: multi-CLI autonomous software factory with deterministic anti-slop gates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="Probe installed coding CLIs and cluster readiness.")
    p_doc.add_argument("--repo", default=".", help="Target repository path.")
    p_doc.set_defaults(func=_cmd_doctor)

    p_init = sub.add_parser("init", help="Scaffold `.code-factory/factory.toml` in a target repo.")
    p_init.add_argument("--repo", default=".", help="Target repository path.")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config.")
    p_init.set_defaults(func=_cmd_init)

    p_gate = sub.add_parser("gate", help="Run deterministic slop, architecture, and repo gates.")
    p_gate.add_argument("--repo", default=".", help="Target repository path.")
    p_gate.add_argument("--base", default=None, help="Git base ref for diff/slop checks.")
    p_gate.add_argument("--check", action="append", help="Additional shell done_check command.")
    p_gate.set_defaults(func=_cmd_gate)

    p_plan = sub.add_parser("plan", help="Synthesize a validated Task DAG using the planner CLI.")
    p_plan.add_argument("--goal", required=True, help="Engineering goal description.")
    p_plan.add_argument("--repo", default=".", help="Target repository path.")
    p_plan.add_argument("--base", default=None, help="Base branch name.")
    p_plan.add_argument("--adapter", default=None, help="Override planner CLI adapter.")
    p_plan.add_argument("--dark", action="store_true", help="Mark DAG for Dark Factory mode.")
    p_plan.add_argument("--out", default=None, help="Output path for tasks.json.")
    p_plan.set_defaults(func=_cmd_plan)

    p_run = sub.add_parser("run", help="Execute a Task DAG through the multi-CLI agent cluster.")
    p_run.add_argument("--dag", required=True, help="Path to tasks.json DAG file.")
    p_run.add_argument("--repo", default=".", help="Target repository path.")
    p_run.add_argument("--dark", action="store_true", help="Auto-merge to base branch if all gates pass.")
    p_run.add_argument("--pr", action="store_true", help="Create a GitHub PR via `gh` when integrated.")
    p_run.add_argument("--keep-worktrees", action="store_true", help="Keep worktrees after run.")
    p_run.add_argument(
        "--no-cross-model",
        action="store_true",
        help="Disable cross-model reviewer exclusion (allow reviewer == worker).",
    )
    p_run.set_defaults(func=_cmd_run)

    p_resume = sub.add_parser(
        "resume",
        help="Resume an interrupted/escalated run, reusing already-PASSED task branches.",
    )
    p_resume.add_argument("run_id", help="Run ID to resume.")
    p_resume.add_argument("--repo", default=".", help="Target repository path.")
    p_resume.add_argument("--pr", action="store_true", help="Create a GitHub PR via `gh` when integrated.")
    p_resume.add_argument("--keep-worktrees", action="store_true", help="Keep worktrees after run.")
    p_resume.add_argument(
        "--no-cross-model",
        action="store_true",
        help="Disable cross-model reviewer exclusion (allow reviewer == worker).",
    )
    p_resume.set_defaults(func=_cmd_resume)

    p_status = sub.add_parser("status", help="Inspect factory runs and state.")
    p_status.add_argument("run_id", nargs="?", default=None, help="Optional run_id.")
    p_status.add_argument("--repo", default=".", help="Target repository path.")
    p_status.set_defaults(func=_cmd_status)

    p_ratchet = sub.add_parser("ratchet", help="Summarize recurring mistakes in the ratchet inbox.")
    p_ratchet.add_argument("--repo", default=".", help="Target repository path.")
    p_ratchet.set_defaults(func=_cmd_ratchet)

    p_audit = sub.add_parser(
        "audit",
        help="Run an architect-guided parallel read-only full-repo code review.",
    )
    p_audit.add_argument("--repo", default=".", help="Target repository path.")
    p_audit.add_argument(
        "--lead",
        default="agy",
        help="Lead architect CLI that plans shards and synthesizes findings (default: agy).",
    )
    p_audit.add_argument(
        "--no-lead",
        action="store_true",
        help="Disable lead architect CLI and use deterministic directory sharding.",
    )
    p_audit.add_argument(
        "--reviewer",
        default="codex",
        help="Parallel read-only reviewer CLI used for heavy shard scanning (default: codex).",
    )
    p_audit.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Maximum parallel shard reviewer workers (default: 3).",
    )
    p_audit.add_argument(
        "--focus",
        default=None,
        help="Optional domain-specific audit focus (e.g. lookahead bias, numerical stability).",
    )
    p_audit.add_argument(
        "--no-synth",
        action="store_true",
        help="Skip final lead architect executive synthesis pass.",
    )
    p_audit.add_argument(
        "--audit-id",
        default=None,
        help="Optional custom audit identifier.",
    )
    p_audit.set_defaults(func=_cmd_audit)

    p_inst = sub.add_parser("install", help="Install plugin/skills into Antigravity, Claude Code, or Codex.")
    p_inst.add_argument(
        "--target",
        choices=["all", "antigravity", "claude", "codex"],
        default="all",
        help="Target agent harness.",
    )
    p_inst.add_argument(
        "--scope",
        choices=["global", "workspace"],
        default="global",
        help="Install globally or into a workspace.",
    )
    p_inst.add_argument("--workspace", default=None, help="Workspace path when --scope workspace.")
    p_inst.add_argument("--copy", action="store_true", help="Copy files instead of symlinking.")
    p_inst.add_argument("--dry-run", action="store_true", help="Print actions without modifying disk.")
    p_inst.set_defaults(func=_cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
