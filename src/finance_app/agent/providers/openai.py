"""OpenAI adapter for `AgentProvider` (ADR-014). Translates the
provider-neutral tool set and conversation history to the Chat Completions
API's wire format, and translates its response back."""

import json
from collections.abc import Sequence
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)

from finance_app.agent.providers.base import (
    AgentMessage,
    AgentTurn,
    FinalMessage,
    ToolCallRequest,
)
from finance_app.agent.tools.base import ToolDefinition


def _tool_to_openai(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _messages_to_openai(
    system_prompt: str, messages: Sequence[AgentMessage]
) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if m.role == "user":
            wire.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant" and m.tool_call is not None:
            wire.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": m.tool_call.call_id,
                            "type": "function",
                            "function": {
                                "name": m.tool_call.tool_name,
                                "arguments": json.dumps(m.tool_call.arguments),
                            },
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            wire.append({"role": "assistant", "content": m.content or ""})
        elif m.role == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content or "",
                }
            )
    return wire


class OpenAIProvider:
    provider_name = "openai"

    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def run_turn(
        self,
        *,
        system_prompt: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
    ) -> AgentTurn:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=cast(
                list[ChatCompletionMessageParam], _messages_to_openai(system_prompt, messages)
            ),
            tools=cast(list[ChatCompletionToolUnionParam], [_tool_to_openai(t) for t in tools]),
        )
        message = response.choices[0].message
        if message.tool_calls:
            # This adapter only ever offers function tools (`_tool_to_openai`),
            # so the response can only contain function tool calls back.
            call = cast(ChatCompletionMessageFunctionToolCall, message.tool_calls[0])
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # A malformed arguments string from the API is treated as
                # "no usable arguments" rather than crashing the turn — the
                # target tool's own required-field validation then raises
                # the same structured ToolInputError an ordinary missing
                # argument would, keeping this on the one error path
                # agent/loop.py already audits and recovers from.
                arguments = {}
            return ToolCallRequest(
                call_id=call.id, tool_name=call.function.name, arguments=arguments
            )
        return FinalMessage(content=message.content or "")
