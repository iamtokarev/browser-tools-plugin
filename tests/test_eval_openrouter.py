"""The OpenRouter client: key lookup, message conversion and reply parsing. No network, only fake values."""

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from eval.agent import MissingApiKey, ToolCall, Usage
from eval.openrouter_model import (
    OpenRouterModel,
    load_api_key,
    parse_reply,
    redact,
    require_api_key,
    to_wire_messages,
    to_wire_tools,
)

FAKE = "fake-value-for-tests"


def test_the_value_comes_from_the_environment_first_then_from_the_env_file(tmp_path: Path):
    env_file = tmp_path / "env-file"
    env_file.write_text("OTHER=1\n# comment\nexport OPENROUTER_API_KEY='from-file'\n")
    assert load_api_key({"OPENROUTER_API_KEY": "from-env"}, env_file) == "from-env"
    assert load_api_key({}, env_file) == "from-file"
    assert load_api_key({}, tmp_path / "missing") is None
    env_file.write_text("OPENROUTER_API_KEY=\nANTHROPIC_API_KEY=other\n")
    assert load_api_key({}, env_file) is None


def test_a_missing_key_gives_a_clear_message(tmp_path: Path):
    with pytest.raises(MissingApiKey, match="OPENROUTER_API_KEY is not set"):
        require_api_key({}, tmp_path / "none")


def test_redact_removes_the_value_from_any_text():
    assert redact(f"bad {FAKE} here", FAKE) == "bad [redacted] here"
    assert redact("text", "") == "text"


def test_tools_and_messages_become_chat_messages_with_images_after_the_tool_messages():
    tools = [{"name": "run", "description": "d", "input_schema": {"type": "object"}}]
    assert to_wire_tools(tools) == [
        {"type": "function", "function": {"name": "run", "description": "d", "parameters": {"type": "object"}}}
    ]
    raw = {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function"}]}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
    result = {"type": "tool_result", "tool_use_id": "c1", "content": [{"type": "text", "text": "label"}, image]}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": raw},
        {"role": "user", "content": [result]},
    ]
    wire = to_wire_messages("SYS", messages)
    assert wire[0] == {"role": "system", "content": "SYS"}
    assert wire[1] == {"role": "user", "content": "task"}
    assert wire[2] is raw
    assert wire[3]["role"] == "tool" and wire[3]["tool_call_id"] == "c1" and wire[3]["content"].startswith("label")
    assert wire[4]["role"] == "user"
    assert wire[4]["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}


def body(finish: str, message: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"finish_reason": finish, "message": message}], "usage": usage}


def test_a_tool_call_reply_gives_calls_and_usage_split_into_plain_written_and_read_tokens():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": '{"code": "1"}'}}],
        "reasoning_details": [{"type": "x"}],
    }
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
    }
    reply = parse_reply(body("tool_calls", message, usage))
    assert reply.stop_reason == "tool_use"
    assert reply.tool_calls == [ToolCall("c1", "run", {"code": "1"})]
    assert reply.usage == Usage(300, 50, 100, 600)
    assert reply.assistant_content["reasoning_details"] == [{"type": "x"}]  # sent back unchanged
    assert "content" not in reply.assistant_content  # a null content is left out


def test_stop_reasons_map_and_bad_arguments_become_an_input_that_the_tool_rejects():
    assert parse_reply(body("stop", {"role": "assistant", "content": "hi"}, {})).stop_reason == "end_turn"
    assert parse_reply(body("length", {"role": "assistant", "content": "h"}, {})).stop_reason == "max_tokens"
    assert parse_reply(body("content_filter", {"role": "assistant", "content": ""}, {})).stop_reason == "refusal"
    bad = {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "run", "arguments": "{oops"}}]}
    reply = parse_reply(body("tool_calls", bad, {}))
    assert reply.tool_calls[0].input == {"_invalid_arguments": "{oops"}


async def test_the_request_holds_no_project_data_and_a_failure_does_not_show_the_value(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[Any] = []

    class Failing(urllib.error.HTTPError):
        def read(self, n: int = -1) -> bytes:
            return f"invalid {FAKE}".encode()

    def fake_open(request: Any, timeout: float) -> Any:
        seen.append(request)
        raise Failing(request.full_url, 401, "Unauthorized", {}, None)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    model = OpenRouterModel("anthropic/claude-sonnet-5.5", key=FAKE)
    with pytest.raises(Exception, match="HTTP 401") as error:
        await model.complete(system="S", messages=[{"role": "user", "content": "P"}], tools=[])
    assert FAKE not in str(error.value)
    (request,) = seen  # a 401 is not tried again
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert sorted(request.headers) == ["Authorization", "Content-type", "User-agent"]  # no Referer, no X-Title
    payload = json.loads(request.data)
    assert payload["model"] == "anthropic/claude-sonnet-5.5"
    assert payload["cache_control"] == {"type": "ephemeral"}
    assert payload["provider"] == {"allow_fallbacks": False}
