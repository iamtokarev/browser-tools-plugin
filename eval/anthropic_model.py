"""The model client for the Anthropic API. It is the only module that imports the SDK."""

import os
from collections.abc import Mapping
from typing import Any, cast

from .agent import Reply, ToolCall, Usage

DEFAULT_MODEL = "claude-sonnet-5-5"
KEY_VARIABLE = "ANTHROPIC_API_KEY"
MAX_OUTPUT_TOKENS = 16_000


class MissingApiKey(Exception):
    pass


def require_api_key(environ: Mapping[str, str] | None = None) -> None:
    """Stop with a clear message when there is no key. Call it before any browser starts."""
    environ = os.environ if environ is None else environ
    if not environ.get(KEY_VARIABLE):
        raise MissingApiKey(
            f"{KEY_VARIABLE} is not set. A real run calls the Anthropic API with your own key and costs money. "
            "Set the variable in your shell, or use --eval-dry-run to check the wiring without a model."
        )


def mcp_tools_to_model_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in tools]


class AnthropicModel:
    """Calls `messages.create` with automatic prompt caching of the growing conversation.

    The `cache_control` argument at the top level caches up to the last block of each request, so a
    long loop pays the full price for each token once. Thinking and effort keep the model defaults.
    There is no fallback model: a fallback would change what the run measures. A refusal ends the run.
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        import anthropic

        self.model = model
        self.client = anthropic.AsyncAnthropic()

    async def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            tools=cast(Any, tools),
            messages=cast(Any, messages),
            cache_control={"type": "ephemeral"},
        )
        usage = response.usage
        return Reply(
            assistant_content=response.content,
            text="".join(block.text for block in response.content if block.type == "text"),
            tool_calls=[
                ToolCall(block.id, block.name, dict(block.input))
                for block in response.content
                if block.type == "tool_use"
            ],
            stop_reason=str(response.stop_reason),
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            ),
        )
