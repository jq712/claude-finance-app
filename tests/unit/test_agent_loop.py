"""Unit tests for the provider-agnostic tool-execution loop (`agent/loop.py`,
ADR-014). A scripted fake `AgentProvider` stands in for a real model, and
the database session is a bare `Mock` — these tests are about the loop's
control flow (when to call a tool, when to stop, what gets audited), not
tool business logic (see tests/integration/test_agent_tools.py) or wire
translation (see tests/unit/test_agent_providers.py).
"""

from typing import Any, cast
from unittest.mock import MagicMock, Mock

from finance_app.agent.loop import MAX_TOOL_CALLS_PER_TURN, run_agent_turn
from finance_app.agent.providers.base import AgentMessage, FinalMessage, ToolCallRequest
from finance_app.db.models.agent import ToolCall


def _mock_session() -> MagicMock:
    """A session double whose `begin_nested()` behaves like a real
    SAVEPOINT context manager: it must not swallow an exception raised
    inside the `with` block. A bare `MagicMock()`'s auto-generated
    `__exit__` returns a (truthy) `MagicMock`, which would silently
    suppress exactly the exceptions `agent/loop.py`'s `except` clauses
    are supposed to catch — defeating these tests without this."""
    session = MagicMock()
    session.begin_nested.return_value.__exit__.return_value = False
    return session


class _ScriptedProvider:
    provider_name = "openai"

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self.calls: list[list[AgentMessage]] = []

    def run_turn(self, *, system_prompt, messages, tools):
        self.calls.append(list(messages))
        return self._turns.pop(0)


def test_final_message_returns_immediately_with_no_tool_calls() -> None:
    session = _mock_session()
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
    session = _mock_session()
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
    session = _mock_session()
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

    session = _mock_session()
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
    session = _mock_session()
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


def test_tool_handler_raising_a_non_tool_input_error_does_not_crash_the_loop() -> None:
    """Regression test (qa-adversarial finding, fixed): `_execute_tool_call`
    used to catch only `ToolInputError` (agent/loop.py). Any other
    exception raised by a handler — e.g. the real `get_category_summary`'s
    former `StopIteration` on an empty period, see
    tests/integration/test_agent_tools.py::
    test_get_category_summary_handles_period_with_no_spending — escaped
    `run_agent_turn` entirely: never audited, never turned into a
    structured error the model can react to, and (via
    `agent/db.py::agent_session_scope`) rolled back the *whole* turn's
    transaction, including any earlier write-tool call already applied
    in this same turn. `cli/main.py`'s `chat` command only catches
    `(OpenAIError, AnthropicError)` around `run_agent_turn`, so it would
    also have killed the entire interactive session, not just the turn.

    Fixed by running each handler inside a SAVEPOINT and broadening the
    catch to `Exception`, turning any handler failure into the same
    structured-error/audited path a `ToolInputError` already took.
    """
    session = _mock_session()
    request = ToolCallRequest(call_id="call_1", tool_name="buggy_tool", arguments={})
    provider = _ScriptedProvider([request, FinalMessage(content="handled gracefully")])

    from unittest.mock import Mock as _Mock

    fake_tool = _Mock()
    fake_tool.name = "buggy_tool"
    fake_tool.is_write = False
    fake_tool.handler = _Mock(side_effect=RuntimeError("boom"))

    import finance_app.agent.loop as loop_module

    original = loop_module.TOOLS_BY_NAME
    loop_module.TOOLS_BY_NAME = {"buggy_tool": fake_tool}
    try:
        history: list[AgentMessage] = []
        reply = run_agent_turn(
            session, provider=provider, system_prompt="p", history=history, user_message="x"
        )
    finally:
        loop_module.TOOLS_BY_NAME = original

    assert reply == "handled gracefully"
    audited = session.add.call_args.args[0]
    assert "error" in audited.result_summary


def test_tool_call_with_list_arguments_instead_of_dict_does_not_crash_the_loop() -> None:
    """Regression test (qa-adversarial finding, fixed): a scripted/buggy
    provider (or a real OpenAI response where `json.loads` on the
    function-call arguments happens to yield a JSON array, e.g. the model
    emits `"[1, 2]"` instead of an object) produces a `ToolCallRequest`
    whose `arguments` is a `list`, not a `dict` — `ToolCallRequest` does
    not validate its own shape. Every handler in `agent/tools/read.py`
    and `write.py` immediately calls `args.get(...)` on this value via
    `_validation.py`'s helpers; a `list` has no `.get`, which used to
    raise an uncaught `AttributeError` rather than a `ToolInputError` —
    same uncaught-exception/rolled-back-transaction/crashed-chat-session
    consequences as the previous test, but reachable via *any* real tool
    with *any* real handler, not just a hypothetical one.

    Exercises the real tool registry (`get_spending_summary`), not a
    mock. Fixed by `_execute_tool_call` rejecting non-`Mapping` arguments
    up front with a structured error, before ever reaching a handler.
    """
    session = _mock_session()
    request = ToolCallRequest(
        call_id="call_1",
        tool_name="get_spending_summary",
        # A real provider's contract promises a dict; this exercises what
        # actually happens if one hands back something else at runtime.
        arguments=cast(dict[str, Any], ["2026-01-01", "2026-01-02"]),
    )
    provider = _ScriptedProvider([request, FinalMessage(content="handled gracefully")])

    history: list[AgentMessage] = []
    reply = run_agent_turn(
        session, provider=provider, system_prompt="p", history=history, user_message="x"
    )

    assert reply == "handled gracefully"


def test_max_tool_calls_override_is_respected() -> None:
    session = _mock_session()
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
