"""The model client for OpenRouter, which speaks the OpenAI chat completions format.

It uses the standard library only. The key never goes into a log, an error or a result file.
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .agent import MissingApiKey, Reply, ToolCall, Usage

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"  # the ID in the OpenRouter model list
KEY_VARIABLE = "OPENROUTER_API_KEY"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MAX_OUTPUT_TOKENS = 16_000
REQUEST_TIMEOUT = 300.0
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4


class ModelError(Exception):
    pass


def load_api_key(environ: Mapping[str, str] | None = None, env_file: Path = ENV_FILE) -> str | None:
    """The key from the environment, else from a `KEY=value` line of the env file. Nothing else is read."""
    environ = os.environ if environ is None else environ
    if value := environ.get(KEY_VARIABLE):
        return value
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() == KEY_VARIABLE:
            return value.strip().strip("'\"") or None
    return None


def require_api_key(environ: Mapping[str, str] | None = None, env_file: Path = ENV_FILE) -> str:
    """Stop with a clear message when there is no key. Call it before any browser starts."""
    key = load_api_key(environ, env_file)
    if not key:
        raise MissingApiKey(
            f"{KEY_VARIABLE} is not set, and the env file has no such line. A real run calls OpenRouter "
            "with your own key and costs money. Set the variable, or use --eval-dry-run to check the wiring."
        )
    return key


def redact(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


# -- The message format --------------------------------------------------------------------


def to_wire_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation of the loop (blocks with a type, as the loop keeps them) as OpenAI chat messages."""
    wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if message["role"] == "assistant":
            wire.append(content)  # the raw message of the reply, with any reasoning details, unchanged
        elif isinstance(content, str):
            wire.append({"role": "user", "content": content})
        else:
            images: list[dict[str, Any]] = []
            for result in content:
                parts = result["content"]
                text = "\n".join(p["text"] for p in parts if p["type"] == "text")
                found = [p for p in parts if p["type"] == "image"]
                if found:
                    text += "\n(The image of this result follows in the next message.)"
                wire.append({"role": "tool", "tool_call_id": result["tool_use_id"], "content": text or "(no content)"})
                for part in found:
                    source = part["source"]
                    url = f"data:{source['media_type']};base64,{source['data']}"
                    images.append({"type": "image_url", "image_url": {"url": url}})
            if images:
                wire.append({"role": "user", "content": [{"type": "text", "text": "Image from the tool:"}, *images]})
    return wire


def to_wire_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def parse_reply(body: dict[str, Any]) -> Reply:
    choice = body["choices"][0]
    message = choice["message"]
    calls = []
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            arguments = {"_invalid_arguments": call["function"]["arguments"]}
        calls.append(ToolCall(call["id"], call["function"]["name"], arguments if isinstance(arguments, dict) else {}))
    finish = choice.get("finish_reason")
    stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(
        finish, str(finish)
    )
    if calls and stop == "end_turn":
        stop = "tool_use"
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    written = details.get("cache_write_tokens") or 0
    # `prompt_tokens` counts the cached and the written tokens too.
    return Reply(
        assistant_content={k: v for k, v in message.items() if v is not None} | {"role": "assistant"},
        text=message.get("content") or "",
        tool_calls=calls,
        stop_reason=stop,
        usage=Usage(
            input_tokens=max(0, (usage.get("prompt_tokens") or 0) - cached - written),
            output_tokens=usage.get("completion_tokens") or 0,
            cache_creation_input_tokens=written,
            cache_read_input_tokens=cached,
        ),
    )


class OpenRouterModel:
    """Calls `chat/completions` with automatic prompt caching and no provider fallback to another model."""

    def __init__(self, model: str = DEFAULT_MODEL, *, key: str | None = None) -> None:
        self.model = model
        self._key = key or require_api_key()

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode()
        last = "no attempt was made"
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(
                f"{BASE_URL}/chat/completions",
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "User-Agent": "browser-tools-eval",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    body = json.loads(response.read())
            except urllib.error.HTTPError as error:
                text = redact(error.read().decode(errors="replace")[:500], self._key)
                last = f"HTTP {error.code}: {text}"
                if error.code not in RETRY_STATUSES:
                    raise ModelError(last) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last = redact(f"{type(error).__name__}: {error}", self._key)
            else:
                if "error" in body and "choices" not in body:
                    raise ModelError(redact(f"OpenRouter error: {json.dumps(body['error'])[:500]}", self._key))
                return body
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(2**attempt)
        raise ModelError(f"No answer after {MAX_ATTEMPTS} attempts. Last problem: {last}")

    async def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Reply:
        payload = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": to_wire_messages(system, messages),
            "tools": to_wire_tools(tools),
            "cache_control": {"type": "ephemeral"},
            "usage": {"include": True},
            "provider": {"allow_fallbacks": False},
        }
        return parse_reply(await asyncio.to_thread(self._post, payload))
