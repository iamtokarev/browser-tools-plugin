"""The result file of a run: one JSON file with the settings, and every run of every task."""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from .agent import AgentResult


@dataclass
class RunRecord:
    task: str
    kind: str
    repeat: int
    passed: bool
    reason: str
    stop: str
    turns: int
    tool_calls: dict[str, int]
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    seconds: float
    error: str | None = None
    final_text: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def make_record(task_id: str, kind: str, repeat: int, result: AgentResult, passed: bool, reason: str) -> RunRecord:
    return RunRecord(
        task=task_id,
        kind=kind,
        repeat=repeat,
        passed=passed,
        reason=reason,
        stop=result.stop,
        turns=result.turns,
        tool_calls=dict(result.tool_calls),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cache_creation_input_tokens=result.usage.cache_creation_input_tokens,
        cache_read_input_tokens=result.usage.cache_read_input_tokens,
        seconds=round(result.seconds, 2),
        error=result.error,
        final_text=result.final_text[:2000],
        steps=[asdict(step) for step in result.steps],
    )


def write_results(path: Path, meta: dict[str, Any], records: list[RunRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = [asdict(r) | {"total_tokens": r.total_tokens} for r in records]
    meta = {"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **meta}
    path.write_text(json.dumps({"meta": meta, "runs": runs}, indent=2) + "\n")


def load_results(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def summarize(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per task: runs, passes, and the mean turns, tokens and seconds."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_task.setdefault(run["task"], []).append(run)
    return {
        task: {
            "runs": len(items),
            "passes": sum(r["passed"] for r in items),
            "turns": mean(r["turns"] for r in items),
            "tokens": mean(r["total_tokens"] for r in items),
            "seconds": mean(r["seconds"] for r in items),
        }
        for task, items in by_task.items()
    }


def token_total(records: list[RunRecord]) -> int:
    return sum(r.total_tokens for r in records)
