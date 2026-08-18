"""The runtime agent's fixed semantic tool vocabulary (ADR-005, ADR-014,
handoff §8.1). `ALL_TOOLS` is the single source of truth every
`AgentProvider` adapter translates to its own wire format — a tool is
added once, in `read.py` or `write.py`, and both providers pick it up.

There is no `run_sql`, no shell, and no filesystem access here, and there
never will be — see tests/security/test_role_grants.py, which fails
loudly if any tool in this package ever grows a query/sql/statement
parameter.
"""

from finance_app.agent.tools.base import ToolDefinition
from finance_app.agent.tools.read import READ_TOOLS
from finance_app.agent.tools.write import WRITE_TOOLS

ALL_TOOLS: list[ToolDefinition] = [*READ_TOOLS, *WRITE_TOOLS]

TOOLS_BY_NAME: dict[str, ToolDefinition] = {tool.name: tool for tool in ALL_TOOLS}

__all__ = ["ALL_TOOLS", "TOOLS_BY_NAME", "ToolDefinition"]
