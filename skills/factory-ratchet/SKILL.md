---
name: factory-ratchet
description: >-
  Inspect recurring agent gate failures and review rejections in
  .code-factory/ratchet_inbox.jsonl and graduate them into permanent
  deterministic checks (slop patterns, AST boundaries, lint rules, or
  regression tests). Use after Code Factory runs to continuously tighten repo
  quality.
---

# Factory Ratchet — Graduate Mistakes into Permanent Rules

Implements @poteto's **Mistake -> Rule -> Gate** feedback loop so an agent never makes the same mistake twice.

> **CLI Resolution**: Use `cf <cmd>` if installed on `PATH`; otherwise run `PYTHONPATH=~/.gemini/config/plugins/code-factory/src python3 -m code_factory <cmd>`.

## Steps

1. **Inspect the Ratchet Inbox**:
   Run `cf ratchet --repo <repo>` to see frequency-ranked gate failures and review rejections.
2. **Classify Each Recurring Pattern**:
   For every pattern seen repeatedly (or any high-severity single failure), pick the **lowest possible layer** of the Inverse Trust Pyramid:
   - **Preferred (Base Layer)**: Add a regex to `[gates.slop].forbid_patterns`, an import boundary to `[[gates.arch.rules]]`, a linter rule, or a type constraint.
   - **Second Choice (Middle Layer)**: Add a fast regression or contract unit test.
   - **Last Resort (Top Layer)**: Only add a 1-line rule to `AGENTS.md` if the constraint genuinely requires abstract human judgment.
3. **Apply & Verify**:
   Edit `.code-factory/factory.toml` (or linter/test files) and run `cf gate --repo <repo>` to confirm the new deterministic guardrail passes on current clean code and catches the regression.
