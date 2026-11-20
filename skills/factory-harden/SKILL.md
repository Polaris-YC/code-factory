---
name: factory-harden
description: >-
  Audit and harden a target Git repository's deterministic guardrails (types,
  linters, tests, architecture boundaries, and slop gates) before running
  autonomous agents. Use when onboarding a new repository to Code Factory or
  when the user asks to harden a codebase against AI slop.
---

# Factory Harden — Build the Inverse Trust Pyramid

Operationalizes @poteto's core rule: **harden the inner loop before scaling throughput.**

## Steps

1. **Scaffold Config**:
   Run `cf init --repo <repo>` to detect baseline test commands and create `<repo>/.code-factory/factory.toml`.
2. **Audit Deterministic Layers**:
   Inspect the repository for four layers of hard constraints:
   - **Fast Tests**: Is there a deterministic, offline unit test command? Add it to `[gates].commands`.
   - **Typecheck & Lint**: Add `mypy`/`pyright`/`ruff`/`tsc`/`eslint`/`cargo clippy`/`go vet` commands if installed in the repo.
   - **Architecture Boundaries**: For Python repos, configure `[[gates.arch.rules]]` to prevent circular or forbidden cross-layer imports.
   - **Footgun / Anti-Slop Patterns**: Add repo-specific dangerous APIs or anti-patterns to `[gates.slop].forbid_patterns` (e.g., raw `eval(`, unguarded `useEffect`, debug prints).
3. **Verify Baseline Green**:
   Run `cf gate --repo <repo>`.
   - Do not enable autonomous or `--dark` runs until `cf gate` passes with exit code `0` on a clean tree.
4. **Report Trust Score**:
   Summarize which deterministic gates are active and any remaining gaps that still rely on review judgment rather than machine checks.
