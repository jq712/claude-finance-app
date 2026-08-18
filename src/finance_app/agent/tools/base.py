"""Provider-neutral semantic tool definitions (ADR-014, handoff §8.4).

A tool is plain Python: a name, a description, a JSON Schema for its
inputs, and a handler. Each `AgentProvider` adapter translates this one
definition into its own wire format at call time — a tool is added once,
here, and both providers pick it up. There is no `run_sql`, no shell, and
no filesystem access, and there never will be (CLAUDE.md, docs/security-model.md).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

ToolHandler = Callable[[Session, Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One semantic tool available to the runtime agent.

    `parameters` is a JSON Schema object — the shape both OpenAI's
    `function.parameters` and Anthropic's `input_schema` expect natively.
    `handler` validates its own inputs (raising `ToolInputError` on
    failure) and returns a JSON-serializable dict. That same dict is both
    the result sent back to the model and the audit `result_summary` —
    nothing extra is ever computed just for the model's benefit (handoff
    §8.2: minimize data sent to the model).

    `is_write` only drives auditing and the loop's write-tool bookkeeping.
    The actual write boundary is the `finance_agent` database role's
    grants (SELECT-only on `plaid.*`; read/write on
    `user.*`/`finance.*`/`agent.*`), enforced independently of this flag
    — see docs/security-model.md invariant 1 and invariant 5.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    is_write: bool = False
