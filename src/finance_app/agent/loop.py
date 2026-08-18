"""The shared tool-execution loop (ADR-014, handoff §8.4). This is the
"everything else" layer that sits above `AgentProvider`: looping until the
model stops requesting tools, writing `agent.tool_calls` audit rows,
enforcing that only registered tools ever execute — written once here,
identical regardless of which adapter is active.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from finance_app.agent.providers.base import (
    AgentMessage,
    AgentProvider,
    FinalMessage,
    ToolCallRequest,
)
from finance_app.agent.tools import ALL_TOOLS, TOOLS_BY_NAME
from finance_app.agent.tools.errors import ToolInputError
from finance_app.db.models.agent import ToolCall

logger = logging.getLogger(__name__)

# A hard ceiling on tool calls within a single user turn. Bounds both
# runaway loops (a model that never converges to a final answer) and the
# blast radius of a prompt-injection attempt that tries to chain many
# write-tool calls from one request.
MAX_TOOL_CALLS_PER_TURN = 8

_BUDGET_EXHAUSTED_MESSAGE = (
    "I wasn't able to finish that within this turn's tool-call budget. "
    "Try asking a narrower question."
)


def _audit_tool_call(
    session: Session,
    *,
    conversation_id: int | None,
    tool_name: str,
    arguments: Mapping[str, Any],
    result: dict[str, Any],
    is_write: bool,
    provider_name: str,
) -> None:
    session.add(
        ToolCall(
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            result_summary=result,
            is_write=is_write,
            provider=provider_name,
        )
    )
    session.flush()


def _execute_tool_call(
    session: Session,
    request: ToolCallRequest,
    *,
    conversation_id: int | None,
    provider_name: str,
) -> dict[str, Any]:
    """Run one requested tool call and audit it, regardless of outcome.

    An unknown tool name or a validation failure both produce a
    structured error result for the model — never an unhandled exception
    that crashes the conversation, and never a call that reaches the
    database without going through a registered tool's own validation.
    """
    tool = TOOLS_BY_NAME.get(request.tool_name)
    if tool is None:
        result: dict[str, Any] = {"error": f"unknown tool {request.tool_name!r}"}
        _audit_tool_call(
            session,
            conversation_id=conversation_id,
            tool_name=request.tool_name,
            arguments=request.arguments,
            result=result,
            is_write=False,
            provider_name=provider_name,
        )
        return result

    try:
        result = tool.handler(session, request.arguments)
    except ToolInputError as exc:
        result = {"error": str(exc)}

    _audit_tool_call(
        session,
        conversation_id=conversation_id,
        tool_name=tool.name,
        arguments=request.arguments,
        result=result,
        is_write=tool.is_write,
        provider_name=provider_name,
    )
    return result


def run_agent_turn(
    session: Session,
    *,
    provider: AgentProvider,
    system_prompt: str,
    history: list[AgentMessage],
    user_message: str,
    conversation_id: int | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS_PER_TURN,
) -> str:
    """Run one user turn to completion.

    Appends `user_message` to `history`, then repeatedly calls
    `provider.run_turn` — executing and auditing any tool call it
    requests — until it returns a final message or `max_tool_calls` is
    exhausted. `history` is mutated in place (including the user message
    and every intermediate tool step) so the caller can inspect or persist
    it; returns the final assistant text.
    """
    history.append(AgentMessage(role="user", content=user_message))

    for _ in range(max_tool_calls):
        turn = provider.run_turn(system_prompt=system_prompt, messages=history, tools=ALL_TOOLS)

        if isinstance(turn, FinalMessage):
            history.append(AgentMessage(role="assistant", content=turn.content))
            return turn.content

        history.append(AgentMessage(role="assistant", tool_call=turn))
        result = _execute_tool_call(
            session,
            turn,
            conversation_id=conversation_id,
            provider_name=provider.provider_name,
        )
        history.append(
            AgentMessage(
                role="tool",
                tool_call_id=turn.call_id,
                tool_name=turn.tool_name,
                content=json.dumps(result, default=str),
            )
        )

    logger.warning(
        "agent turn exhausted tool-call budget",
        extra={"conversation_id": conversation_id, "max_tool_calls": max_tool_calls},
    )
    history.append(AgentMessage(role="assistant", content=_BUDGET_EXHAUSTED_MESSAGE))
    return _BUDGET_EXHAUSTED_MESSAGE
