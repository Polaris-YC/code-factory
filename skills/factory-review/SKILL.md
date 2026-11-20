---
name: factory-review
description: >-
  Run an adversarial anti-slop code review against a diff or branch using a
  strict rubric (deletion bias, scope discipline, comment hygiene, edge-case
  attacks) and emit a structured ReviewVerdict JSON. Use when reviewing agent
  diffs or acting as the cross-model reviewer in Code Factory.
---

# Factory Review — Adversarial Anti-Slop Rubric ("Arena")

Act as a skeptical principal engineer whose job is to **reject AI slop before it lands**.

## Rubric

1. **Correctness & Edge Cases**:
   - Does the diff actually fulfill the specification and handle boundary/error states?
   - Are tests asserting real behavior (falsifiable), or are they tautological mocks?
2. **Deletion Bias**:
   - Can the same outcome be achieved with less code?
   - Reject speculative abstractions, unused helpers, and redundant wrapper layers.
3. **Scope Discipline**:
   - Reject unrelated formatting churn, drive-by refactors, or edits outside declared `owned_paths`.
4. **Comment & Prose Hygiene**:
   - Reject comments that narrate *what* obvious code does or rationalize hacks/workarounds.
5. **Deterministic Gate Evidence**:
   - Verify that all configured gates passed; never waive a failing deterministic check.

## Required Output Format

Return strictly valid JSON:

```json
{
  "decision": "APPROVE",
  "summary": "Concise 1-2 sentence verdict.",
  "issues": [
    {
      "severity": "high",
      "file": "src/example.py",
      "line": 42,
      "rule": "deletion-bias",
      "message": "Unnecessary wrapper class duplicates stdlib behavior.",
      "fix_hint": "Inline the 4-line function directly at the call site."
    }
  ]
}
```

## Full-Repo Architect-Guided Audit (`cf audit`)

> **CLI Resolution**: Use `cf <cmd>` if installed on `PATH`; otherwise run `PYTHONPATH=~/.gemini/config/plugins/code-factory/src python3 -m code_factory <cmd>`.

To audit an entire repository in read-only mode (e.g., using `agy` as Audit Director to partition shards & synthesize findings while parallel `codex` workers do the heavy line-by-line scanning):

```bash
cf audit --repo /path/to/repo --lead agy --reviewer codex --workers 4 --focus "<domain risks>"
```

Outputs are written to `<repo>/.code-factory/audits/<audit_id>/audit_report.md` and `audit_data.json`.
