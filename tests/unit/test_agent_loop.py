"""Unit tests for the provider-agnostic tool-execution loop (`agent/loop.py`,
ADR-014). A scripted fake `AgentProvider` stands in for a real model, and
the database session is a bare `Mock` — these tests are about the loop's
control flow (when to call a tool, when to stop, what gets audited), not
tool business logic (see tests/integration/test_agent_tools.py) or wire
translation (see tests/unit/test_agent_providers.py).
"""

from unittest.mock import Mock

from finance_app.agent.loop import MAX_TOOL_CALLS_PER_TURN, run_agent_turn
from finance_app.agent.providers.base import AgentMessage, FinalMessage, ToolCallRequest
from finance_app.db.models.agent import ToolCall


class _ScriptedProvider:
    provider_name = "openai"

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self.calls: list[list[AgentMessage]] = []

    def run_turn(self, *, system_prompt, messages, tools):
        self.calls.append(list(messages))
        return self._turns.pop(0)


def test_final_message_returns_immediately_with_no_tool_calls() -> None:
    session = Mock()
    provider = _ScriptedProvider([FinalMessage(content="Hi there.")])
    history: list[AgentMessage] = []

    reply = run_agent_turn(
        session,
        provider=provider,
        system_prompt="p",
        history=history,
        user_message="hello",
    )

    assert reply == "Hi there."
    assert history[0] == AgentMessage(role="user", content="hello")
    assert history[-1] == AgentMessage(role="assistant", content="Hi there.")
    session.add.assert_not_called()


def test_tool_call_executes_audits_and_feeds_result_back(monkeypatch) -> None:
    session = Mock()
    request = ToolCallRequest(
        call_id="call_1", tool_name="get_spending_summary", arguments={"start": "a"}
    )
    provider = _ScriptedProvider([request, FinalMessage(content="You spent $10.")])

    fake_tool = Mock()
    fake_tool.name = "get_spending_summary"
    fake_tool.is_write = False
    fake_tool.handler = Mock(return_value={"total_spent": "10.00"})
    monkeypatch.setattr("finance_app.agent.loop.TOOLS_BY_NAME", {"get_spending_summary": fake_tool})

    history: list[AgentMessage] = []
    reply = run_agent_turn(
        session,
        provider=provider,
        system_prompt="p",
        history=history,
        user_message="How much did I spend?",
        conversation_id=42,
    )

    assert reply == "You spent $10."
    fake_tool.handler.assert_called_once_with(session, {"start": "a"})

    audited = session.add.call_args.args[0]
    assert isinstance(audited, ToolCall)
    assert audited.tool_name == "get_spending_summary"
    assert audited.provider == "openai"
    assert audited.is_write is False
    assert audited.conversation_id == 42
    assert audited.result_summary == {"total_spent": "10.00"}

    tool_message = [m for m in history if m.role == "tool"][0]
    assert tool_message.tool_call_id == "call_1"
    assert tool_message.content is not None
    assert '"total_spent": "10.00"' in tool_message.content


def test_unknown_tool_name_is_audited_as_an_error_not_a_crash() -> None:
    session = Mock()
    request = ToolCallRequest(call_id="call_1", tool_name="delete_everything", arguments={})
    provider = _ScriptedProvider([request, FinalMessage(content="Can't do that.")])

    history: list[AgentMessage] = []
    reply = run_agent_turn(
        session, provider=provider, system_prompt="p", history=history, user_message="do it"
    )

    assert reply == "Can't do that."
    audited = session.add.call_args.args[0]
    assert audited.tool_name == "delete_everything"
    assert "error" in audited.result_summary
    assert audited.is_write is False


def test_tool_input_error_becomes_a_structured_result_not_an_exception(monkeypatch) -> None:
    from finance_app.agent.tools.errors import ToolInputError

    session = Mock()
    request = ToolCallRequest(call_id="call_1", tool_name="create_budget", arguments={})
    provider = _ScriptedProvider([request, FinalMessage(content="Please provide an amount.")])

    fake_tool = Mock()
    fake_tool.name = "create_budget"
    fake_tool.is_write = True
    fake_tool.handler = Mock(side_effect=ToolInputError("'monthly_amount' is required"))
    monkeypatch.setattr("finance_app.agent.loop.TOOLS_BY_NAME", {"create_budget": fake_tool})

    history: list[AgentMessage] = []
    reply = run_agent_turn(
        session, provider=provider, system_prompt="p", history=history, user_message="budget please"
    )

    assert reply == "Please provide an amount."
    audited = session.add.call_args.args[0]
    assert audited.result_summary == {"error": "'monthly_amount' is required"}
    assert audited.is_write is True  # audited as a write attempt even though it failed


def test_tool_call_budget_exhaustion_returns_a_fallback_message() -> None:
    session = Mock()
    request = ToolCallRequest(call_id="call_1", tool_name="get_spending_summary", arguments={})
    provider = _ScriptedProvider([request] * MAX_TOOL_CALLS_PER_TURN)

    fake_tool = Mock()
    fake_tool.name = "get_spending_summary"
    fake_tool.is_write = False
    fake_tool.handler = Mock(return_value={})

    import finance_app.agent.loop as loop_module

    original = loop_module.TOOLS_BY_NAME
    loop_module.TOOLS_BY_NAME = {"get_spending_summary": fake_tool}
    try:
        history: list[AgentMessage] = []
        reply = run_agent_turn(
            session,
            provider=provider,
            system_prompt="p",
            history=history,
            user_message="loop forever",
        )
    finally:
        loop_module.TOOLS_BY_NAME = original

    assert "tool-call budget" in reply
    assert len(provider.calls) == MAX_TOOL_CALLS_PER_TURN
    assert history[-1] == AgentMessage(role="assistant", content=reply)


def test_max_tool_calls_override_is_respected() -> None:
    session = Mock()
    request = ToolCallRequest(call_id="call_1", tool_name="get_spending_summary", arguments={})
    provider = _ScriptedProvider([request, request, FinalMessage(content="done")])

    fake_tool = Mock()
    fake_tool.name = "get_spending_summary"
    fake_tool.is_write = False
    fake_tool.handler = Mock(return_value={})

    import finance_app.agent.loop as loop_module

    original = loop_module.TOOLS_BY_NAME
    loop_module.TOOLS_BY_NAME = {"get_spending_summary": fake_tool}
    try:
        history: list[AgentMessage] = []
        reply = run_agent_turn(
            session,
            provider=provider,
            system_prompt="p",
            history=history,
            user_message="x",
            max_tool_calls=1,
        )
    finally:
        loop_module.TOOLS_BY_NAME = original

    assert "tool-call budget" in reply
    assert len(provider.calls) == 1
