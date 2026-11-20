---
name: code-factory
description: >-
  Orchestrate autonomous multi-CLI software development across codex, claude,
  agy, and custom coding CLIs with isolated git worktrees, deterministic
  anti-slop gates, and cross-model adversarial review. Use when the user asks
  to run a development task through Code Factory or delegate multi-step coding
  work with minimal human intervention.
---

# Code Factory — Master Orchestrator ("Factory Foreman")

This skill turns the host agent into the **Factory Foreman**, coordinating a heterogeneous cluster of coding CLIs via the deterministic `cf` (`python3 -m code_factory`) engine.

## Workflow

### 1. Preflight & Trust Pyramid Check
1. Probe cluster readiness:
   `cf doctor --repo <repo>` (or `PYTHONPATH=<factory_src> python3 -m code_factory doctor --repo <repo>`).
2. Verify `<repo>/.code-factory/factory.toml` exists.
   - If missing, activate the `factory-harden` skill first (or run `cf init --repo <repo>`) so the repository has deterministic guardrails before spawning parallel agents.
   - Remember: **automating a sloppy inner loop only scales the slop.**

### 2. Synthesize a Falsifiable Task DAG
Decompose the user's goal into a crisp `tasks.json` following [task_dag_schema.md](./references/task_dag_schema.md) and [playbooks.md](./references/playbooks.md):
- Every parallel task **must** have disjoint `owned_paths` globs.
- Use `depends_on` when tasks touch shared interfaces or depend on upstream outputs.
- Every task **must** include falsifiable `done_checks` (or rely on verified repo gates).
- You may also generate a draft DAG via `cf plan --goal "<goal>" --repo <repo> --out /tmp/tasks.json`.

### 3. Human Checkpoint (Software Factory Default)
- **Default (`software` mode)**: Present the concise Task DAG (task IDs, `owned_paths`, `done_checks`, assigned adapters) for a single go/no-go approval, unless the user explicitly requested autonomous / `--dark` execution.
- **Dark mode (`--dark`)**: Skip the checkpoint only when the user explicitly requests dark-factory mode and deterministic gates are verified green.

### 4. Execute Autonomous Multi-CLI Cluster
Run the deterministic orchestrator:
```bash
cf run --dag /tmp/tasks.json --repo <repo> [--pr] [--dark]
```
The engine handles the entire inner loop with zero babysitting:
- Creates isolated `git worktree`s per task.
- Runs worker CLIs in parallel topological waves.
- Runs deterministic gates (`slop-gate`, `arch-gate`, repo tests/linters, `done_checks`) and feeds exact diagnostics back to the worker on failure (up to `max_gate_retries`).
- Runs cross-model adversarial review (using a different CLI than the worker when >= 2 CLIs are available) and loops on `REQUEST_CHANGES`.
- Integrates passed branches onto `cf/<run_id>/integrate` and re-runs full-repo gates.

### 5. Report Evidence & Ratchet
Read `<repo>/.code-factory/runs/<run_id>/report.md` (and `escalation.md` if any task escalated):
- Present verifiable gate evidence, commit SHAs, integration branch / PR link.
- If any gate failures or review rejections occurred, run `cf ratchet --repo <repo>` and suggest running `factory-ratchet` to graduate repeated mistakes into permanent rules.
