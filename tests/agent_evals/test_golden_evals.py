"""The Milestone 6 regression harness (handoff §24/§29): every eval case
in `evals/cases.py`, run against every configured `AgentProvider`, against
a fresh copy of the golden dataset. Skips entirely (see conftest.py) when
no provider credential is configured — this suite calls a real model and
must never silently spend API budget in ordinary CI runs.

A prompt, tool, or model-config change that regresses one provider while
leaving the other passing shows up here as a single failing case listing
exactly which provider(s) failed — the exit criterion in handoff §29.
"""

import pytest
from evals.cases import CASES, EvalCase, EvalRun
from sqlalchemy import select

from finance_app.agent.loop import run_agent_turn
from finance_app.agent.prompts import system_prompt_for
from finance_app.agent.providers.base import AgentMessage
from finance_app.db.models.agent import Conversation, ToolCall

pytestmark = pytest.mark.agent_eval


def _run_case(session, provider, case: EvalCase, conversation_ids: list[int]) -> EvalRun:
    conversation = Conversation()
    session.add(conversation)
    session.flush()
    conversation_ids.append(conversation.id)

    history: list[AgentMessage] = []
    reply = run_agent_turn(
        session,
        provider=provider,
        system_prompt=system_prompt_for(provider.provider_name),
        history=history,
        user_message=case.prompt,
        conversation_id=conversation.id,
    )
    session.commit()

    tool_calls = (
        session.execute(
            select(ToolCall)
            .where(ToolCall.conversation_id == conversation.id)
            .order_by(ToolCall.id)
        )
        .scalars()
        .all()
    )
    return EvalRun(reply=reply, tool_calls=tool_calls, session=session)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_golden_eval_case(case: EvalCase, eval_providers, golden_dataset) -> None:
    failures: list[str] = []
    for provider in eval_providers:
        with golden_dataset() as (session, dataset, conversation_ids):
            try:
                run = _run_case(session, provider, case, conversation_ids)
                case.check(run, dataset)
            except AssertionError as exc:
                failures.append(f"[{provider.provider_name}] {exc}")
            except Exception as exc:  # noqa: BLE001 - a provider/SDK failure for
                # one provider (bad credential, rate limit, network) must not
                # abort the whole parametrized case before the other
                # configured provider gets a chance to run.
                failures.append(f"[{provider.provider_name}] {type(exc).__name__}: {exc}")

    if failures:
        message = "\n".join(failures)
        if case.critical:
            pytest.fail(message)
        pytest.xfail(message)
