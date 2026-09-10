"""Proves docs/security-model.md invariant 5 and invariant 1 mechanically:
`finance_agent` can read Plaid facts but cannot write them, and can freely
read/write its own interpretation layer. This is the test the handoff
requires before Milestone 1 is considered done — grants, not convention.
"""

import pytest
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration


def test_finance_agent_can_select_plaid_transactions(role_engine) -> None:
    engine = role_engine("finance_agent")
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT count(*) FROM plaid.transactions")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO plaid.transactions (account_id, plaid_transaction_id, amount, date, name) "
        "VALUES (1, 'blocked', 1.00, now(), 'blocked')",
        "UPDATE plaid.transactions SET amount = 0",
        "DELETE FROM plaid.transactions",
    ],
    ids=["insert", "update", "delete"],
)
def test_finance_agent_cannot_mutate_plaid_transactions(role_engine, statement: str) -> None:
    engine = role_engine("finance_agent")
    with engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
        conn.exec_driver_sql(statement)


@pytest.mark.parametrize("table", ["items", "accounts", "sync_state"])
@pytest.mark.parametrize("verb", ["DELETE FROM", "UPDATE", "INSERT INTO"])
def test_finance_agent_cannot_write_any_plaid_table(role_engine, table: str, verb: str) -> None:
    engine = role_engine("finance_agent")
    statement = {
        "DELETE FROM": f"DELETE FROM plaid.{table}",
        "UPDATE": f"UPDATE plaid.{table} SET id = id",
        "INSERT INTO": f"INSERT INTO plaid.{table} DEFAULT VALUES",
    }[verb]
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql(statement)


def test_finance_agent_can_write_user_schema(role_engine) -> None:
    engine = role_engine("finance_agent")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "INSERT INTO \"user\".preferences (key, value) VALUES ('security_test', '{}') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        conn.commit()
        conn.exec_driver_sql("DELETE FROM \"user\".preferences WHERE key = 'security_test'")
        conn.commit()


def test_finance_agent_can_write_finance_schema(role_engine) -> None:
    engine = role_engine("finance_agent")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "INSERT INTO finance.budgets (category, monthly_amount) "
            "VALUES ('security_test', 1.00) "
            "ON CONFLICT (category) DO UPDATE SET monthly_amount = EXCLUDED.monthly_amount"
        )
        conn.commit()
        conn.exec_driver_sql("DELETE FROM finance.budgets WHERE category = 'security_test'")
        conn.commit()


def test_finance_agent_can_write_agent_schema(role_engine) -> None:
    engine = role_engine("finance_agent")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "INSERT INTO agent.conversations (summary) VALUES ('security test') RETURNING id"
        )
        conn.commit()
        conn.exec_driver_sql("DELETE FROM agent.conversations WHERE summary = 'security test'")
        conn.commit()


def test_finance_agent_has_no_access_to_ops_schema(role_engine) -> None:
    engine = role_engine("finance_agent")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("SELECT count(*) FROM ops.sync_runs")


def test_finance_observer_is_read_only_on_ops(role_engine) -> None:
    engine = role_engine("finance_observer")
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT count(*) FROM ops.job_runs")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("INSERT INTO ops.job_runs (job_name) VALUES ('blocked')")


def test_finance_observer_has_no_access_to_plaid_schema(role_engine) -> None:
    engine = role_engine("finance_observer")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("SELECT count(*) FROM plaid.transactions")


def test_finance_backup_can_read_every_schema_but_not_write(role_engine) -> None:
    engine = role_engine("finance_backup")
    with engine.connect() as conn:
        for schema, table in [
            ("plaid", "transactions"),
            ("user", "preferences"),
            ("finance", "budgets"),
            ("agent", "conversations"),
            ("ops", "job_runs"),
        ]:
            conn.exec_driver_sql(f'SELECT count(*) FROM "{schema}".{table}')
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("INSERT INTO ops.job_runs (job_name) VALUES ('blocked')")


def test_no_arbitrary_sql_tool_exists_in_the_agent_tool_registry() -> None:
    """Static guard for docs/security-model.md invariant 2. There is no
    tools package yet (Milestone 5 builds it) — this test exists now so
    it starts failing loudly the moment one is added carelessly."""
    import importlib.util

    spec = importlib.util.find_spec("finance_app.agent.tools")
    if spec is None:
        pytest.skip("agent.tools package does not exist yet (Milestone 5)")

    import inspect

    tools_module = importlib.import_module("finance_app.agent.tools")
    for name, obj in inspect.getmembers(tools_module):
        assert "run_sql" not in name.lower(), f"found a SQL-shaped tool: {name}"
        if inspect.isfunction(obj):
            params = set(inspect.signature(obj).parameters)
            assert not {"query", "sql", "statement"} & params, (
                f"{name} takes a raw query/sql/statement parameter — "
                "the agent must use semantic tools only"
            )
