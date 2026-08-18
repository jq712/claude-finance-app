"""The provider-neutral seam (ADR-014, handoff §8.4).

`AgentProvider` has exactly one job: given the conversation so far and the
available tools, make one model call and return either "call this tool
with this input" or "here is the final message to the user." Everything
else — looping until the model stops requesting tools, writing
`agent.tool_calls` audit rows, enforcing the write-tool permission
boundary, persisting conversation history — lives in `agent/loop.py`,
above this interface, and is identical regardless of which adapter is
active.

`AgentMessage` is the provider-neutral conversation-history representation
the loop maintains. Each adapter translates the whole history to its own
wire format at every call (stateless conversion, no adapter-side session
state) — see `providers/openai.py` and `providers/anthropic.py`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from finance_app.agent.tools.base import ToolDefinition

AgentRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """The model's request to call one tool. `call_id` is the provider's
    own identifier for this call and must be threaded back unchanged on
    the matching tool-result message — both OpenAI and Anthropic use it to
    line up a result with its request."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FinalMessage:
    """The model's final answer to the user for this turn."""

    content: str


AgentTurn = ToolCallRequest | FinalMessage


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """One entry in the provider-neutral conversation history.

    - `role="user"`: `content` is the user's text.
    - `role="assistant"`, `content` set: a final assistant message.
    - `role="assistant"`, `tool_call` set: the assistant requested a tool call.
    - `role="tool"`: the result of executing `tool_call_id`/`tool_name`,
      JSON-encoded in `content`.

    Exactly one of (`content`, `tool_call`) is meaningful per message; the
    combination is enforced by how `agent/loop.py` constructs these, not
    by the type itself.
    """

    role: AgentRole
    content: str | None = None
    tool_call: ToolCallRequest | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None


class AgentProvider(Protocol):
    """One concrete model backend (OpenAI or Anthropic). Adapters hold no
    conversation state between calls — `messages` is the complete history
    each time."""

    provider_name: str

    def run_turn(
        self,
        *,
        system_prompt: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
    ) -> AgentTurn: ...
