"""The eval agent loop, driven by a scripted model and a fake tool session. No model, no browser."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mcp_types import CallToolResult, ImageContent, TextContent

from eval.agent import Reply, ToolCall, Usage, mcp_tools_to_model_tools, run_agent, tool_result_blocks

TOOLS = [{"name": "run", "description": "d", "input_schema": {"type": "object"}}]


def use(name: str, args: dict[str, Any], *, id: str = "t1", usage: Usage | None = None) -> Reply:
    return Reply([{"type": "tool_use"}], "", [ToolCall(id, name, args)], "tool_use", usage or Usage())


def answer(text: str, usage: Usage | None = None) -> Reply:
    return Reply([{"type": "text", "text": text}], text, [], "end_turn", usage or Usage())


class ScriptedModel:
    def __init__(self, replies: list[Reply | Exception]) -> None:
        self.replies = list(replies)
        self.seen: list[dict[str, Any]] = []

    async def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        self.seen.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeSession:
    def __init__(self, handler: Callable[[str, dict[str, Any]], CallToolResult] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handler = handler or (lambda name, args: CallToolResult(content=[TextContent(type="text", text="ok")]))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.calls.append((name, arguments))
        return self.handler(name, arguments)


async def drive(model: ScriptedModel, session: FakeSession | None = None, max_turns: int = 30):
    return await run_agent(
        model=model, session=session or FakeSession(), tools=TOOLS, system="S", prompt="P", max_turns=max_turns
    )


async def test_counts_turns_tool_calls_and_every_kind_of_token():
    model = ScriptedModel(
        [
            use("run", {"code": "1"}, usage=Usage(100, 10, 5, 0)),
            use("run", {"code": "2"}, usage=Usage(20, 12, 0, 105)),
            answer('{"a": 1}', Usage(30, 8, 0, 125)),
        ]
    )
    result = await drive(model)
    assert (result.stop, result.turns, result.final_text) == ("answered", 3, '{"a": 1}')
    assert dict(result.tool_calls) == {"run": 2}
    assert result.usage == Usage(150, 30, 5, 230)
    assert result.usage.total == 415
    assert [step.tool for step in result.steps] == ["run", "run"]


async def test_the_model_gets_the_system_prompt_the_tools_and_each_tool_result():
    model = ScriptedModel([use("run", {"code": "1"}), answer("done")])
    await drive(model)
    first, second = model.seen
    assert first["system"] == "S" and first["tools"] == TOOLS
    assert first["messages"] == [{"role": "user", "content": "P"}]
    last = second["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["tool_use_id"] == "t1"
    assert last["content"][0]["content"] == [{"type": "text", "text": "ok"}]


async def test_the_turn_limit_stops_a_model_that_never_answers():
    model = ScriptedModel([use("run", {}, id=f"t{i}") for i in range(10)])
    result = await drive(model, max_turns=3)
    assert (result.stop, result.turns) == ("turn_limit", 3)
    assert len(result.steps) == 3
    assert len(model.replies) == 7  # no model call after the limit


async def test_a_tool_error_is_passed_to_the_model_and_marked():
    session = FakeSession(lambda *_: CallToolResult(content=[TextContent(type="text", text="boom")], is_error=True))
    model = ScriptedModel([use("run", {}), answer("x")])
    result = await drive(model, session)
    assert model.seen[1]["messages"][-1]["content"][0]["is_error"] is True
    assert result.steps[0].is_error and result.steps[0].result == "boom"


async def test_a_protocol_failure_in_a_tool_call_is_an_error_result_not_a_crash():
    def fail(*_: Any) -> CallToolResult:
        raise RuntimeError("connection closed")

    result = await drive(ScriptedModel([use("run", {}), answer("x")]), FakeSession(fail))
    assert result.stop == "answered"
    assert "connection closed" in result.steps[0].result


async def test_an_unknown_tool_is_an_error_result_and_not_sent_to_the_server():
    session = FakeSession()
    model = ScriptedModel([use("navigate", {}), answer("x")])
    result = await drive(model, session)
    assert session.calls == []
    assert model.seen[1]["messages"][-1]["content"][0]["is_error"] is True
    assert dict(result.tool_calls) == {"navigate": 1}


async def test_a_model_error_ends_the_run_and_keeps_what_was_counted():
    model = ScriptedModel([use("run", {}, usage=Usage(10, 1)), RuntimeError("overloaded")])
    result = await drive(model)
    assert result.stop == "model_error"
    assert result.error == "RuntimeError: overloaded"
    assert result.usage == Usage(10, 1)


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
async def test_a_stop_without_a_tool_call_or_an_answer_is_recorded_as_such(reason: str):
    reply = Reply([{"type": "text"}], "cut", [], reason, Usage())
    result = await drive(ScriptedModel([reply]))
    assert result.stop == reason


async def test_a_paused_turn_is_sent_back_and_counts_as_a_turn():
    pause = Reply([], "", [], "pause_turn", Usage())
    result = await drive(ScriptedModel([pause, answer("x")]))
    assert (result.stop, result.turns) == ("answered", 2)


def test_images_and_text_become_model_blocks():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="Screenshot: viewport"),
            ImageContent(type="image", data="AAAA", mime_type="image/jpeg"),
        ]
    )
    assert tool_result_blocks(result) == [
        {"type": "text", "text": "Screenshot: viewport"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
    ]
    assert tool_result_blocks(CallToolResult(content=[])) == [{"type": "text", "text": "(no content)"}]


def test_mcp_tools_become_model_tools():
    tool = SimpleNamespace(name="snapshot", description=None, input_schema={"type": "object"})
    assert mcp_tools_to_model_tools([tool]) == [
        {"name": "snapshot", "description": "", "input_schema": {"type": "object"}}
    ]


async def test_a_reply_cut_at_the_token_limit_runs_no_tool():
    cut = Reply([{"type": "tool_use"}], "", [ToolCall("t1", "run", {"code": "retu"})], "max_tokens", Usage())
    session = FakeSession()
    result = await drive(ScriptedModel([cut]), session)
    assert result.stop == "max_tokens"
    assert session.calls == []
