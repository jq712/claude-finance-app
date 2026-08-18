"""Conversation persistence (handoff §8.3): "persist only what is
useful". Only the user's text and the assistant's final answer for each
turn are written to `agent.messages` — the intermediate tool-call/result
steps within a turn are ephemeral scratch work, already captured
durably and in more useful form by the `agent.tool_calls` audit trail
(`agent/loop.py`), and are not replayed as conversation history on the
next turn.
"""

import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.agent.providers.base import AgentMessage, AgentRole
from finance_app.db.models.agent import Conversation, Message


def start_conversation(session: Session) -> Conversation:
    conversation = Conversation()
    session.add(conversation)
    session.flush()
    return conversation


def end_conversation(session: Session, conversation: Conversation) -> None:
    conversation.ended_at = datetime.datetime.now(datetime.UTC)
    session.flush()


def append_turn(
    session: Session, *, conversation_id: int, user_message: str, assistant_message: str
) -> None:
    session.add(Message(conversation_id=conversation_id, role="user", content=user_message))
    session.add(
        Message(conversation_id=conversation_id, role="assistant", content=assistant_message)
    )
    session.flush()


def load_history(session: Session, *, conversation_id: int) -> list[AgentMessage]:
    """The durable `user`/`assistant` text history for a conversation, in
    order — the starting point `run_agent_turn` appends this turn's
    messages onto."""
    rows = (
        session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        )
        .scalars()
        .all()
    )
    return [AgentMessage(role=cast(AgentRole, row.role), content=row.content) for row in rows]
