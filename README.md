# Code Factory

**Code Factory** is a universal multi-repository autonomous software factory packaged as a **portable Skill Suite + Antigravity Plugin** backed by a **zero-dependency Python 3.12 deterministic orchestration engine (`cf`)**.

Inspired by **@poteto (Lauren Tan)'s** talk *"All Code Is Slop (Except Mine)"*:

> *"Automating a sloppy inner loop just scales the slop."*

Code Factory pairs a **heterogeneous multi-CLI agent cluster** (`codex`, `claude`, `agy`, and any custom CLI via TOML templates) with an **Inverse Trust Pyramid** of deterministic guardrails so agents can run in parallel isolated `git worktree`s with minimal human input.

---

## Architecture

1. **Layer 1 — Portable Skill Suite & Plugin (`plugin.json`, `skills/`, `rules/AGENTS.md`)**
   - `code-factory`: Master Foreman skill (preflight -> Task DAG -> `cf run` -> evidence report).
   - `factory-harden`: Audits & hardens a target repo's deterministic gates before unleashing agents.
   - `factory-review`: Adversarial anti-slop review rubric (`ReviewVerdict` JSON).
   - `factory-ratchet`: Graduates recurring failures from `.code-factory/ratchet_inbox.jsonl` into permanent deterministic rules.
2. **Layer 2 — Deterministic Python 3.12 Engine (`src/code_factory/`)**
   - **Unified CLI Adapters**: `codex`, `claude`, `agy`, `generic` (any CLI via `factory.toml`), and `mock` (offline testing).
   - **Git Worktree Isolation**: Parallel wave execution with strict disjoint `owned_paths` validation and clean branch integration.
   - **Deterministic Gates**: Built-in `slop-gate` (scope, diff budget, comment ratio, forbidden patterns), built-in Python AST `arch-gate` (directional import boundaries), repo commands, and task `done_checks`.
   - **Bounded Autonomous Loop**: Gate failures and cross-model review rejections automatically feed machine diagnostics back to the worker without waking the human; escalates cleanly (`escalation.md`) only when retries are exhausted.
   - **Software Factory Default + `--dark` Opt-In**: Defaults to producing a verified integration branch / PR for human merge; `--dark` merges directly to `base_branch` only when all tasks, cross-agent reviews, and integrated gates pass.

---

## Quickstart

Run directly without installing external dependencies:

```bash
# 1. Check installed coding CLIs and cluster readiness
PYTHONPATH=src python3 -m code_factory doctor --repo /path/to/target-repo

# 2. Initialize deterministic config in a target repo
PYTHONPATH=src python3 -m code_factory init --repo /path/to/target-repo

# 3. Verify deterministic gates (slop, arch, tests)
PYTHONPATH=src python3 -m code_factory gate --repo /path/to/target-repo

# 4. Run a Task DAG across the multi-CLI cluster
PYTHONPATH=src python3 -m code_factory run --dag tasks.json --repo /path/to/target-repo

# 5. Install plugin/skills for Antigravity, Claude Code, and Codex
PYTHONPATH=src python3 -m code_factory install --target all --scope global --dry-run
```

Or install in editable mode to use the `cf` command directly:

```bash
pip install --no-build-isolation -e .
cf doctor
```

---

## Adding Any Custom CLI Adapter

Plug any non-interactive CLI (`gemini`, `opencode`, `aider`, custom wrapper) into `.code-factory/factory.toml` with zero Python code:

```toml
[adapters.gemini]
type = "generic"
command = ["gemini", "-p", "-"]
prompt_mode = "stdin"

[routing]
worker = ["codex", "gemini", "claude", "agy"]
reviewer = ["claude", "agy", "codex", "gemini"]
```

---

## Running Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
