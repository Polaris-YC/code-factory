"""Anti-slop ratchet inbox: record recurring failures to graduate into permanent rules."""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from code_factory.core.models import utc_now_iso


@dataclass(frozen=True)
class RatchetEntry:
    run_id: str
    task_id: str
    source: str
    rule: str
    summary: str
    excerpt: str = ""
    files: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now_iso)


class RatchetInbox:
    def __init__(self, repo_root: Path) -> None:
        self.path = repo_root.resolve() / ".code-factory" / "ratchet_inbox.jsonl"
        self._lock = threading.Lock()

    def record(self, entry: RatchetEntry) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def read_all(self) -> list[RatchetEntry]:
        if not self.path.is_file():
            return []
        entries: list[RatchetEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries.append(RatchetEntry(**data))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    def summarize(self) -> str:
        entries = self.read_all()
        if not entries:
            return "Ratchet inbox is empty. No recorded gate failures or review rejections."

        counts = Counter((e.source, e.rule) for e in entries)
        lines = [
            f"Ratchet Inbox ({len(entries)} total event(s)):",
            "",
        ]
        for (source, rule), count in counts.most_common():
            latest = next(
                e for e in reversed(entries) if e.source == source and e.rule == rule
            )
            lines.append(f"- [{source}] `{rule}` — {count}x (latest: {latest.summary})")
        lines.append("")
        lines.append(
            "Tip: Run the `factory-ratchet` skill to graduate repeated items into "
            "permanent deterministic gates in `.code-factory/factory.toml`."
        )
        return "\n".join(lines)
