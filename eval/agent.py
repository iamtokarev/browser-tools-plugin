"""The agent loop: one model, one MCP session, a turn limit, and counts of everything.

The loop owns the counting. A turn is one model call. Tokens come from the `usage` object of each
reply, so they include the tool list, the system prompt and every earlier message that the model
read again. The model sits behind the small `ModelClient` protocol, so a test can use a scripted fake.
"""

import base64
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mcp_types import CallToolResult, ImageContent, TextContent

Stop = Literal["answered", "turn_limit", "max_tokens", "refusal", "model_error", "other"]
STEP_TEXT_LIMIT = 400  # characters of a tool input or result kept in the record


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens

    @property
    def total(self) -> int:
        """All tokens that the model read or wrote, cached or not."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens + self.output_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Reply:
    """One model answer. `assistant_content` goes back into the conversation unchanged."""

    assistant_content: Any
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage


class ModelClient(Protocol):
    async def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply: ...


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult: ...


@dataclass
class Step:
    tool: str
    input: str
    result: str
    is_error: bool


@dataclass
class AgentResult:
    stop: Stop
    final_text: str
    turns: int
    tool_calls: Counter[str] = field(default_factory=Counter)
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0
    steps: list[Step] = field(default_factory=list)
    error: str | None = None


def tool_result_blocks(result: CallToolResult) -> list[dict[str, Any]]:
    """MCP content as the blocks of a model `tool_result`: text and images."""
    blocks: list[dict[str, Any]] = []
    for part in result.content:
        if isinstance(part, TextContent):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageContent):
            data = part.data if isinstance(part.data, str) else base64.b64encode(part.data).decode()
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.mime_type, "data": data}})
    return blocks or [{"type": "text", "text": "(no content)"}]


def result_text(result: CallToolResult) -> str:
    return "\n".join(part.text for part in result.content if isinstance(part, TextContent))


def _cut(text: str) -> str:
    return text if len(text) <= STEP_TEXT_LIMIT else text[:STEP_TEXT_LIMIT] + "..."


async def run_agent(
    *,
    model: ModelClient,
    session: ToolSession,
    tools: list[dict[str, Any]],
    system: str,
    prompt: str,
    max_turns: int,
) -> AgentResult:
    """Ask the model until it answers without a tool call, or until `max_turns` model calls were made."""
    started = time.monotonic()
    result = AgentResult(stop="other", final_text="", turns=0)
    known = {tool["name"] for tool in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    while True:
        if result.turns >= max_turns:
            result.stop = "turn_limit"
            break
        result.turns += 1
        try:
            reply = await model.complete(system=system, messages=messages, tools=tools)
        except Exception as error:
            result.stop = "model_error"
            result.error = f"{type(error).__name__}: {error}"
            break
        result.usage.add(reply.usage)
        messages.append({"role": "assistant", "content": reply.assistant_content})
        if reply.stop_reason in ("end_turn", "stop_sequence") and not reply.tool_calls:
            result.stop = "answered"
            result.final_text = reply.text
            break
        if reply.stop_reason == "pause_turn":
            continue
        if not reply.tool_calls:
            result.stop = (
                "max_tokens"
                if reply.stop_reason == "max_tokens"
                else "refusal"
                if reply.stop_reason == "refusal"
                else "other"
            )
            result.final_text = reply.text
            break
        outputs: list[dict[str, Any]] = []
        for call in reply.tool_calls:
            result.tool_calls[call.name] += 1
            if call.name not in known:
                blocks = [{"type": "text", "text": f"Unknown tool: {call.name}"}]
                result.steps.append(Step(call.name, _cut(str(call.input)), blocks[0]["text"], True))
                outputs.append({"type": "tool_result", "tool_use_id": call.id, "content": blocks, "is_error": True})
                continue
            try:
                tool_result = await session.call_tool(call.name, call.input)
            except Exception as error:
                tool_result = CallToolResult(
                    content=[TextContent(type="text", text=f"{type(error).__name__}: {error}")], is_error=True
                )
            result.steps.append(
                Step(call.name, _cut(str(call.input)), _cut(result_text(tool_result)), bool(tool_result.is_error))
            )
            outputs.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": tool_result_blocks(tool_result),
                    "is_error": bool(tool_result.is_error),
                }
            )
        messages.append({"role": "user", "content": outputs})
    result.seconds = time.monotonic() - started
    return result
