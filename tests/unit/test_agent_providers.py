"""Unit tests for the OpenAI/Anthropic `AgentProvider` adapters (ADR-014).
No network or database involved — the SDK clients are replaced with fakes
that assert on the translated wire format and hand back a minimal
response shape, so these tests are about the *translation*, not the SDKs
themselves.
"""

import json
from types import SimpleNamespace

from finance_app.agent.providers.anthropic import AnthropicProvider
from finance_app.agent.providers.base import AgentMessage, FinalMessage, ToolCallRequest
from finance_app.agent.providers.openai import OpenAIProvider
from finance_app.agent.tools.base import ToolDefinition

_TOOL = ToolDefinition(
    name="get_spending_summary",
    description="Total spending in a period.",
    parameters={
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
        "required": ["start", "end"],
        "additionalProperties": False,
    },
    handler=lambda session, args: {},
)


# --- OpenAI --------------------------------------------------------------


def _openai_final_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))]
    )


def _openai_tool_call_response(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
                        )
                    ],
                )
            )
        ]
    )


def test_openai_provider_translates_final_message() -> None:
    provider = OpenAIProvider(api_key="test", model="gpt-4o")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _openai_final_response("You spent $42.50.")

    provider._client.chat.completions.create = fake_create  # type: ignore[method-assign]

    turn = provider.run_turn(
        system_prompt="be helpful",
        messages=[AgentMessage(role="user", content="How much did I spend?")],
        tools=[_TOOL],
    )

    assert turn == FinalMessage(content="You spent $42.50.")
    assert captured["messages"][0] == {"role": "system", "content": "be helpful"}
    assert captured["messages"][1] == {"role": "user", "content": "How much did I spend?"}
    assert captured["tools"][0]["function"]["name"] == "get_spending_summary"
    assert captured["tools"][0]["type"] == "function"


def test_openai_provider_translates_tool_call_request() -> None:
    provider = OpenAIProvider(api_key="test", model="gpt-4o")
    provider._client.chat.completions.create = lambda **kwargs: _openai_tool_call_response(  # type: ignore[method-assign]
        "call_1", "get_spending_summary", {"start": "2026-08-01", "end": "2026-08-31"}
    )

    turn = provider.run_turn(
        system_prompt="be helpful",
        messages=[AgentMessage(role="user", content="How much did I spend in August?")],
        tools=[_TOOL],
    )

    assert turn == ToolCallRequest(
        call_id="call_1",
        tool_name="get_spending_summary",
        arguments={"start": "2026-08-01", "end": "2026-08-31"},
    )


def test_openai_provider_round_trips_a_tool_result_message() -> None:
    """A prior assistant tool-call plus its tool-result message must
    translate to the exact `tool_calls`/`tool_call_id` linkage OpenAI
    requires to associate a result with its request."""
    provider = OpenAIProvider(api_key="test", model="gpt-4o")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _openai_final_response("Done.")

    provider._client.chat.completions.create = fake_create  # type: ignore[method-assign]

    history = [
        AgentMessage(role="user", content="How much did I spend?"),
        AgentMessage(
            role="assistant",
            tool_call=ToolCallRequest(
                call_id="call_1", tool_name="get_spending_summary", arguments={"start": "a"}
            ),
        ),
        AgentMessage(
            role="tool", tool_call_id="call_1", tool_name="get_spending_summary", content='{"x": 1}'
        ),
    ]
    provider.run_turn(system_prompt="p", messages=history, tools=[_TOOL])

    wire = captured["messages"]
    assert wire[2]["tool_calls"][0]["id"] == "call_1"
    assert wire[2]["tool_calls"][0]["function"]["name"] == "get_spending_summary"
    assert wire[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"x": 1}'}


# --- Anthropic -------------------------------------------------------------


def _anthropic_text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _anthropic_tool_use_response(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments)]
    )


def test_anthropic_provider_translates_final_message() -> None:
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-5")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _anthropic_text_response("You spent $42.50.")

    provider._client.messages.create = fake_create  # type: ignore[method-assign]

    turn = provider.run_turn(
        system_prompt="be helpful",
        messages=[AgentMessage(role="user", content="How much did I spend?")],
        tools=[_TOOL],
    )

    assert turn == FinalMessage(content="You spent $42.50.")
    assert captured["system"] == "be helpful"
    assert captured["messages"] == [{"role": "user", "content": "How much did I spend?"}]
    assert captured["tools"][0]["name"] == "get_spending_summary"
    assert captured["tools"][0]["input_schema"] == _TOOL.parameters


def test_anthropic_provider_translates_tool_call_request() -> None:
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-5")
    provider._client.messages.create = lambda **kwargs: _anthropic_tool_use_response(  # type: ignore[method-assign]
        "toolu_1", "get_spending_summary", {"start": "2026-08-01", "end": "2026-08-31"}
    )

    turn = provider.run_turn(
        system_prompt="be helpful",
        messages=[AgentMessage(role="user", content="How much did I spend in August?")],
        tools=[_TOOL],
    )

    assert turn == ToolCallRequest(
        call_id="toolu_1",
        tool_name="get_spending_summary",
        arguments={"start": "2026-08-01", "end": "2026-08-31"},
    )


def test_anthropic_provider_encodes_tool_result_as_a_user_message() -> None:
    """Anthropic has no dedicated "tool" role — a tool result must become
    a `user` message carrying a `tool_result` block, keyed by
    `tool_use_id`, or the API will reject the conversation."""
    provider = AnthropicProvider(api_key="test", model="claude-sonnet-4-5")
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _anthropic_text_response("Done.")

    provider._client.messages.create = fake_create  # type: ignore[method-assign]

    history = [
        AgentMessage(role="user", content="How much did I spend?"),
        AgentMessage(
            role="assistant",
            tool_call=ToolCallRequest(
                call_id="toolu_1", tool_name="get_spending_summary", arguments={"start": "a"}
            ),
        ),
        AgentMessage(
            role="tool",
            tool_call_id="toolu_1",
            tool_name="get_spending_summary",
            content='{"x": 1}',
        ),
    ]
    provider.run_turn(system_prompt="p", messages=history, tools=[_TOOL])

    wire = captured["messages"]
    assert wire[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"x": 1}'}],
    }
