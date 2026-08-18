"""Fixtures for the Milestone 6 golden-dataset agent evals (handoff §24).

These tests call a real, configured `AgentProvider` — they are the only
tests in this repository that spend real API budget — so the whole
session is skipped with a clear reason when neither `OPENAI_API_KEY` nor
`ANTHROPIC_API_KEY` is set, rather than failing CI on every PR. See
`docs/financial-agent.md`'s eval framework section for how to run them
locally/in staging with real credentials.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from evals.fixtures.golden_dataset import GoldenDataset, cleanup_golden_dataset, seed_golden_dataset
from sqlalchemy import delete
from sqlalchemy.orm import Session

from finance_app.agent.providers.anthropic import AnthropicProvider
from finance_app.agent.providers.base import AgentProvider
from finance_app.agent.providers.openai import OpenAIProvider
from finance_app.config.settings import Settings
from finance_app.db.models.agent import Conversation, ToolCall


@pytest.fixture(scope="session")
def eval_providers() -> list[AgentProvider]:
    """Every `AgentProvider` this environment has credentials for —
    independent of `AGENT_PROVIDER`, since the whole point of running
    evals per-provider (handoff §24 exit criteria) is to catch a change
    that regresses one provider while the other still passes."""
    settings = Settings()
    providers: list[AgentProvider] = []
    if settings.openai_api_key.get_secret_value():
        providers.append(
            OpenAIProvider(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.openai_model or "gpt-4o",
            )
        )
    if settings.anthropic_api_key.get_secret_value():
        providers.append(
            AnthropicProvider(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.anthropic_model or "claude-sonnet-4-5",
            )
        )
    if not providers:
        pytest.skip(
            "agent evals require OPENAI_API_KEY and/or ANTHROPIC_API_KEY to be set "
            "(they call a real model) — see docs/financial-agent.md"
        )
    return providers


@contextmanager
def _golden_dataset_session(role_engine) -> Iterator[tuple[Session, GoldenDataset, list[int]]]:
    """One golden dataset, seeded fresh via `finance_app` (the only role
    allowed to write `plaid.*`) and torn down on exit, plus a
    `finance_agent`-role session to run the eval turn against — the same
    role the real `finance chat` connection uses (`agent/db.py`)."""
    app_engine = role_engine("finance_app")
    app_session = Session(app_engine)
    dataset = seed_golden_dataset(app_session)
    app_session.commit()

    agent_engine = role_engine("finance_agent")
    agent_session = Session(agent_engine)
    conversation_ids: list[int] = []
    try:
        yield agent_session, dataset, conversation_ids
    finally:
        agent_session.rollback()
        agent_session.close()
        if conversation_ids:
            app_session.execute(
                delete(ToolCall).where(ToolCall.conversation_id.in_(conversation_ids))
            )
            app_session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
        cleanup_golden_dataset(app_session, dataset)
        app_session.close()


@pytest.fixture
def golden_dataset(role_engine):
    """Factory fixture: `with golden_dataset() as (session, dataset,
    conversation_ids): ...` — a fresh golden dataset per `with` block, so
    each provider in a multi-provider eval case gets an isolated copy
    (write-tool cases mutate state)."""

    def _make():
        return _golden_dataset_session(role_engine)

    return _make
