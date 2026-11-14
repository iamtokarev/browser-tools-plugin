"""Options and the run state of the eval. Only `pytest eval` loads this file."""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .agent import MissingApiKey, ModelClient
from .guidance import SKILL_PATH, Setup, SkillMissing, read_skill
from .openrouter_model import DEFAULT_MODEL, ENV_FILE, OpenRouterModel, require_api_key
from .results import RunRecord, token_total, write_results
from .site import FixtureSite
from .tasks import TASKS, TASKS_BY_ID, Task

EVAL_RUN = pytest.StashKey["EvalRun"]()
RESULTS_DIR = Path(__file__).parent / "results"  # ignored by git


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("eval", "browser tools eval")
    group.addoption("--eval-setup", choices=[s.value for s in Setup], default=Setup.NONE.value)
    group.addoption("--eval-model", default=DEFAULT_MODEL, help="The OpenRouter model ID.")
    group.addoption("--eval-env-file", default=str(ENV_FILE), help="Read OPENROUTER_API_KEY from here if not set.")
    group.addoption("--eval-repeats", type=int, default=3)
    group.addoption("--eval-max-turns", type=int, default=30)
    group.addoption("--eval-max-tokens", type=int, default=5_000_000, help="Stop starting new runs above this total.")
    group.addoption("--eval-tasks", default="", help="Task IDs, separated by commas. Default: all.")
    group.addoption("--eval-out", default="", help="Result file. Default: eval/results/<time>-<setup>.json")
    group.addoption("--eval-skill-path", default=str(SKILL_PATH))
    group.addoption("--eval-dry-run", action="store_true", help="Use a scripted model. No key, no cost.")


@dataclass
class EvalRun:
    setup: Setup
    api_key: str = field(repr=False)
    model_name: str
    repeats: int
    max_turns: int
    max_tokens: int
    dry_run: bool
    skill_path: Path
    out: Path
    records: list[RunRecord] = field(default_factory=list)

    def write(self) -> None:
        meta = {
            "setup": self.setup.value,
            "model": "scripted" if self.dry_run else self.model_name,
            "dry_run": self.dry_run,
            "repeats": self.repeats,
            "max_turns": self.max_turns,
        }
        write_results(self.out, meta, self.records)


def selected_tasks(config: pytest.Config) -> list[Task]:
    wanted = [name.strip() for name in config.getoption("--eval-tasks").split(",") if name.strip()]
    unknown = [name for name in wanted if name not in TASKS_BY_ID]
    if unknown:
        raise pytest.UsageError(f"Unknown eval tasks: {unknown}. Known: {sorted(TASKS_BY_ID)}")
    return [TASKS_BY_ID[name] for name in wanted] or list(TASKS)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if {"task", "repeat"} <= set(metafunc.fixturenames):
        cases = [
            (t, r) for t in selected_tasks(metafunc.config) for r in range(metafunc.config.getoption("--eval-repeats"))
        ]
        metafunc.parametrize(("task", "repeat"), cases, ids=[f"{t.id}-{r + 1}" for t, r in cases])


@pytest.fixture(scope="session")
def eval_run(request: pytest.FixtureRequest) -> EvalRun:
    """The settings of the run. It stops the session before any browser starts when the key is missing."""
    config = request.config
    dry_run = config.getoption("--eval-dry-run")
    api_key = ""
    if not dry_run:
        try:
            api_key = require_api_key(os.environ, Path(config.getoption("--eval-env-file")))
        except MissingApiKey as error:
            pytest.exit(str(error), returncode=2)
    setup = Setup(config.getoption("--eval-setup"))
    out = config.getoption("--eval-out") or str(
        RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{setup.value}{'-dry' if dry_run else ''}.json"
    )
    run = EvalRun(
        setup=setup,
        api_key=api_key,
        model_name=config.getoption("--eval-model"),
        repeats=config.getoption("--eval-repeats"),
        max_turns=config.getoption("--eval-max-turns"),
        max_tokens=config.getoption("--eval-max-tokens"),
        dry_run=dry_run,
        skill_path=Path(config.getoption("--eval-skill-path")),
        out=Path(out),
    )
    config.stash[EVAL_RUN] = run
    return run


@pytest.fixture(scope="session")
def real_model(eval_run: EvalRun) -> ModelClient:
    return OpenRouterModel(eval_run.model_name, key=eval_run.api_key)


@pytest.fixture
def skill_available(eval_run: EvalRun) -> None:
    if eval_run.setup is Setup.SKILL:
        try:
            read_skill(eval_run.skill_path)
        except SkillMissing as error:
            pytest.skip(f"{error} Write the skill (ticket 34) or pass --eval-skill-path.")


def make_model_factory(
    eval_run: EvalRun, task: Task, request: pytest.FixtureRequest
) -> Callable[[FixtureSite], ModelClient]:
    from .oracle import OracleModel

    if eval_run.dry_run:
        return lambda site: OracleModel(task, site)
    model = request.getfixturevalue("real_model")
    return lambda site: model


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    run = config.stash.get(EVAL_RUN, None)
    if run is None or not run.records:
        return
    passed = sum(r.passed for r in run.records)
    totals = {
        "input": sum(r.input_tokens for r in run.records),
        "output": sum(r.output_tokens for r in run.records),
        "cache write": sum(r.cache_creation_input_tokens for r in run.records),
        "cache read": sum(r.cache_read_input_tokens for r in run.records),
    }
    terminalreporter.write_sep("-", "eval summary")
    terminalreporter.write_line(f"setup={run.setup.value} model={'scripted' if run.dry_run else run.model_name}")
    terminalreporter.write_line(f"passed {passed} of {len(run.records)} runs")
    terminalreporter.write_line(
        f"tokens in total: {token_total(run.records)} ({', '.join(f'{k} {v}' for k, v in totals.items())})"
    )
    terminalreporter.write_line(f"result file: {run.out}")
