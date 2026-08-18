"""Anthropic (Claude API) adapter for `AgentProvider` (ADR-014). This is
the *runtime* financial agent when `AGENT_PROVIDER=anthropic` — a
different system from Claude Code, the engineering agent building this
application, even though both may be Claude-family models. See CLAUDE.md
and ADR-014's "naming risk" consequence.

Translates the provider-neutral tool set and conversation history to the
Messages API's wire format, and translates its response back. Anthropic
has no separate "tool" role: a tool result is a `user` message carrying a
`tool_result` content block, and a tool-call request is an `assistant`
message carrying a `tool_use` content block.
"""

from collections.abc import Sequence
from typing import Any, cast

from anthropic import Anthropic
from anthropic.types import MessageParam, ToolUnionParam

from finance_app.agent.providers.base import (
    AgentMessage,
    AgentTurn,
    FinalMessage,
    ToolCallRequest,
)
from finance_app.agent.tools.base import ToolDefinition

_DEFAULT_MAX_TOKENS = 4096


def _tool_to_anthropic(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def _messages_to_anthropic(messages: Sequence[AgentMessage]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            wire.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant" and m.tool_call is not None:
            wire.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": m.tool_call.call_id,
                            "name": m.tool_call.tool_name,
                            "input": m.tool_call.arguments,
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            wire.append({"role": "assistant", "content": m.content or ""})
        elif m.role == "tool":
            wire.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content or "",
                        }
                    ],
                }
            )
    return wire


class AnthropicProvider:
    provider_name = "anthropic"

    def __init__(self, *, api_key: str, model: str, max_tokens: int = _DEFAULT_MAX_TOKENS) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def run_turn(
        self,
        *,
        system_prompt: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
    ) -> AgentTurn:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt,
            messages=cast(list[MessageParam], _messages_to_anthropic(messages)),
            tools=cast(list[ToolUnionParam], [_tool_to_anthropic(t) for t in tools]),
        )
        for block in response.content:
            if block.type == "tool_use":
                return ToolCallRequest(
                    call_id=block.id,
                    tool_name=block.name,
                    arguments=dict(block.input),
                )
        text = "".join(block.text for block in response.content if block.type == "text")
        return FinalMessage(content=text)
