"""Compare two result files: `uv run python -m eval.compare before.json after.json`."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .results import load_results, summarize


def _delta(before: float, after: float, digits: int = 1) -> str:
    return f"{after - before:+.{digits}f}"


def compare(before_path: Path, after_path: Path) -> str:
    before, after = load_results(before_path), load_results(after_path)
    lines: list[str] = []
    for label, data, path in (("before", before, before_path), ("after", after, after_path)):
        meta = data["meta"]
        lines.append(
            f"{label}: {path.name}  setup={meta.get('setup')}  model={meta.get('model')}  dry_run={meta.get('dry_run')}"
        )
    for key in ("model", "setup", "max_turns"):
        if before["meta"].get(key) != after["meta"].get(key):
            lines.append(f"note: the two runs differ in {key}: {before['meta'].get(key)} and {after['meta'].get(key)}")
    if before["meta"].get("dry_run") or after["meta"].get("dry_run"):
        lines.append("warning: a dry run holds no model data. Its numbers say nothing about a model.")
    a, b = summarize(before["runs"]), summarize(after["runs"])
    common = [task for task in a if task in b]
    for task in sorted(set(a) ^ set(b)):
        lines.append(f"note: task {task} is in one file only and is left out")
    lines.append("")
    header = f"{'task':<18} {'pass':>9} {'turns':>15} {'tokens':>23} {'seconds':>15}"
    lines.append(header)
    for task in common:
        x, y = a[task], b[task]
        lines.append(
            f"{task:<18} {x['passes']}/{x['runs']}->{y['passes']}/{y['runs']:<2}"
            f" {x['turns']:>5.1f}->{y['turns']:<5.1f}{_delta(x['turns'], y['turns']):>6}"
            f" {x['tokens']:>8.0f}->{y['tokens']:<8.0f}{_delta(x['tokens'], y['tokens'], 0):>7}"
            f" {x['seconds']:>5.1f}->{y['seconds']:<5.1f}{_delta(x['seconds'], y['seconds']):>6}"
        )
    if common:
        runs_a = sum(a[t]["runs"] for t in common)
        runs_b = sum(b[t]["runs"] for t in common)
        pass_a = sum(a[t]["passes"] for t in common)
        pass_b = sum(b[t]["passes"] for t in common)
        tokens_a = sum(a[t]["tokens"] * a[t]["runs"] for t in common)
        tokens_b = sum(b[t]["tokens"] * b[t]["runs"] for t in common)
        lines.append("")
        lines.append(f"pass rate: {pass_a}/{runs_a} -> {pass_b}/{runs_b}")
        lines.append(f"tokens in total: {tokens_a:.0f} -> {tokens_b:.0f} ({_delta(tokens_a, tokens_b, 0)})")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.compare", description="Compare two eval result files.")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        print(compare(args.before, args.after))
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot compare: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
