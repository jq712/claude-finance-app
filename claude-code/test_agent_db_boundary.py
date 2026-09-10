"""Proves the runtime agent's own database connection (`agent/db.py`) is
bound to the least-privilege `finance_agent` role, not `finance_app` or
`finance_owner`. This is the mechanism ADR-014/handoff §7.1 depend on:
every tool handler in `agent/tools/` runs against a session from
`agent/db.py`, so if that connection were ever pointed at a broader role,
the `finance_agent`-grants boundary tests in test_role_grants.py would
stop meaning anything for the agent's actual runtime behavior.
"""

import pytest
from sqlalchemy import text

from finance_app.agent.db import agent_session_scope, dispose_agent_engine

pytestmark = pytest.mark.integration


def test_agent_session_scope_connects_as_finance_agent() -> None:
    with agent_session_scope() as session:
        current_user = session.execute(text("SELECT current_user")).scalar_one()
    assert current_user == "finance_agent"
    dispose_agent_engine()


def test_agent_session_scope_cannot_write_plaid() -> None:
    with (
        agent_session_scope() as session,
        pytest.raises(Exception, match="permission denied"),
    ):
        session.execute(text("DELETE FROM plaid.transactions"))
    dispose_agent_engine()
