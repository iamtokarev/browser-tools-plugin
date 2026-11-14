"""A scripted model that plays the solution of a task. It is the fake for the loop tests and the dry run."""

import json
from typing import Any

from .agent import Reply, ToolCall, Usage
from .site import FixtureSite
from .tasks import OracleStep, Task


def last_result_text(messages: list[dict[str, Any]]) -> str:
    """The text of the tool results in the last user message, or an empty text at the start."""
    content = messages[-1]["content"]
    if isinstance(content, str):
        return ""
    return "\n".join(
        block["text"] for result in content for block in result.get("content", []) if block.get("type") == "text"
    )


class OracleModel:
    """Calls the tools of the scripted solution one by one, then answers.

    `problems` lists each step whose result did not hold the text that the solution expects.
    """

    def __init__(self, task: Task, site: FixtureSite, *, final_text: str | None = None) -> None:
        self.steps: list[OracleStep] = task.oracle(site)
        if final_text is None:
            final_text = json.dumps(task.answer(site)) if task.answer else '{"done": true}'
        self.final_text = final_text
        self.problems: list[str] = []
        self._turn = 0

    async def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        previous = last_result_text(messages)
        if self._turn > 0:
            step = self.steps[self._turn - 1]
            if step.expect is not None and step.expect not in previous:
                self.problems.append(
                    f"step {self._turn} ({step.tool}) did not return {step.expect!r}: {previous[:300]!r}"
                )
        usage = Usage()
        if self._turn >= len(self.steps):
            return Reply(self.final_text, self.final_text, [], "end_turn", usage)
        step = self.steps[self._turn]
        self._turn += 1
        args = step.args if isinstance(step.args, dict) else step.args(previous)
        call = ToolCall(f"oracle-{self._turn}", step.tool, args)
        block = {"type": "tool_use", "id": call.id, "name": call.name, "input": args}
        return Reply([block], "", [call], "tool_use", usage)
