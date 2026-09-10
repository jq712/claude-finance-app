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
    arguments: object,
    result: dict[str, Any],
    is_write: bool,
    provider_name: str,
) -> None:
    # `arguments` comes straight from the provider and is not guaranteed to
    # be a dict (see the malformed-shape guard in `_execute_tool_call`) —
    # auditing must never itself raise on a value it's only trying to
    # record.
    try:
        stored_arguments = dict(arguments)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        stored_arguments = {"_unparseable_arguments": repr(arguments)[:500]}
    session.add(
        ToolCall(
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=stored_arguments,
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

    An unknown tool name, malformed arguments, a validation failure, or
    any other exception a handler raises all produce a structured error
    result for the model — never an unhandled exception that crashes the
    conversation. The handler itself runs inside a SAVEPOINT so a failure
    partway through a write (e.g. a constraint violation surfacing from
    `session.flush()`) rolls back only this tool call, not the rest of
    this turn's already-applied work.
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

    if not isinstance(request.arguments, Mapping):
        result = {"error": "tool arguments must be a JSON object"}
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

    try:
        with session.begin_nested():
            result = tool.handler(session, request.arguments)
    except ToolInputError as exc:
        result = {"error": str(exc)}
    except Exception:
        # A bug in a tool handler (or an unexpected DB error surfacing
        # from it) must degrade to a structured result the model can see
        # and the audit trail can record — not crash the whole turn, and
        # not the entire interactive `chat` session sitting above it.
        # Full detail goes to the operational log, never to the model.
        logger.exception(
            "tool handler raised an unexpected exception",
            extra={"tool_name": tool.name, "conversation_id": conversation_id},
        )
        result = {"error": "an internal error occurred while running this tool"}

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
