"""Run every task `--eval-repeats` times. Start it by hand: `uv run --group eval pytest eval`. See eval/README.md."""

import pytest

from .conftest import EvalRun, make_model_factory
from .runner import run_task
from .tasks import Task


@pytest.mark.usefixtures("skill_available")
async def test_task(task: Task, repeat: int, eval_run: EvalRun, request: pytest.FixtureRequest) -> None:
    used = sum(r.total_tokens for r in eval_run.records)
    if used >= eval_run.max_tokens:
        pytest.skip(f"The token total {used} reached --eval-max-tokens {eval_run.max_tokens}.")
    record = await run_task(
        task,
        repeat=repeat,
        setup=eval_run.setup,
        model_for=make_model_factory(eval_run, task, request),
        max_turns=eval_run.max_turns,
        skill_path=eval_run.skill_path,
    )
    eval_run.records.append(record)
    eval_run.write()  # after every run, so that an interrupted run keeps its data
    print(
        f"\n{task.id} #{repeat + 1}: {'PASS' if record.passed else 'FAIL'} ({record.reason}) "
        f"turns={record.turns} tokens={record.total_tokens} seconds={record.seconds}"
    )
    assert record.passed, record.reason
