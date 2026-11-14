"""Run one task once: a fresh fixture site, a fresh server over stdio, one agent loop, one check."""

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from .agent import AgentResult, ModelClient, Usage, mcp_tools_to_model_tools, run_agent
from .guidance import SKILL_PATH, Setup, build_system_prompt
from .oracle import OracleModel
from .results import RunRecord, make_record
from .site import FixtureSite
from .tasks import Task, Verdict, evaluate

SERVER_COMMAND = [sys.executable, "-c", "from browser_tools_plugin import main; main()"]
RUN_TIME_LIMIT = 1200.0  # seconds for one run, the browser start included


def server_environment(data_dir: Path, site: FixtureSite) -> dict[str, str]:
    """The environment of the server process.

    It never holds the real data directory, the user's own settings or a model key. The site limits
    allow the fixture origin only, so the agent cannot open a page on the internet.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("BROWSER_TOOLS_", "ANTHROPIC_")) and name not in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")
    }
    return env | {
        "PLUGIN_DATA": str(data_dir),
        "BROWSER_TOOLS_THROWAWAY_PROFILE": "true",
        "BROWSER_TOOLS_ALLOW_SITES": site.origin,
    }


async def run_task(
    task: Task,
    *,
    repeat: int,
    setup: Setup,
    model_for: Callable[[FixtureSite], ModelClient],
    max_turns: int,
    skill_path: Path = SKILL_PATH,
) -> RunRecord:
    """The skill file must exist when the setup needs it: the caller checks that before this call."""
    with FixtureSite(seed=repeat + 1) as site, tempfile.TemporaryDirectory(prefix="browser-tools-eval-") as data:
        model = model_for(site)
        params = StdioServerParameters(
            command=SERVER_COMMAND[0], args=SERVER_COMMAND[1:], env=server_environment(Path(data), site)
        )
        try:
            with anyio.fail_after(RUN_TIME_LIMIT):
                async with Client(params) as client:
                    tools = mcp_tools_to_model_tools((await client.list_tools()).tools)
                    system = build_system_prompt(setup, client.instructions, skill_path)
                    result = await run_agent(
                        model=model,
                        session=client,
                        tools=tools,
                        system=system,
                        prompt=task.prompt(site),
                        max_turns=max_turns,
                    )
        except TimeoutError:
            result = AgentResult(stop="other", final_text="", turns=0, usage=Usage(), error="run time limit reached")
        verdict = evaluate(task, site, result.final_text)
        if isinstance(model, OracleModel) and model.problems:
            verdict = Verdict(False, "The scripted solution broke: " + "; ".join(model.problems))
        if verdict.passed and result.stop != "answered":
            verdict = Verdict(False, f"The run did not end with an answer (stop: {result.stop}).")
        if result.error and not verdict.passed:
            verdict = Verdict(False, f"{verdict.reason} ({result.error})")
        return make_record(task.id, task.kind, repeat, result, verdict.passed, verdict.reason)
