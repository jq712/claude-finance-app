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


def test_finance_observer_has_no_access_to_plaid_financial_data(role_engine) -> None:
    """migrations/versions/0004 (Milestone 7) gives finance_observer a
    narrow, table-specific grant on plaid.items/plaid.sync_state for
    `finops sync-status` — deliberately *not* the rest of the schema.
    This proves the boundary stayed narrow: transactions/accounts, which
    hold real financial data, remain unreadable."""
    engine = role_engine("finance_observer")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("SELECT count(*) FROM plaid.transactions")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("SELECT count(*) FROM plaid.accounts")


def test_finance_observer_can_read_sync_status_metadata_only(role_engine) -> None:
    """The narrow exception migrations/versions/0004 adds: `finops
    sync-status` (src/finance_app/ops/status.py) needs Item status and
    cursor-presence metadata, not transaction content."""
    engine = role_engine("finance_observer")
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT count(*) FROM plaid.items")
        conn.exec_driver_sql("SELECT count(*) FROM plaid.sync_state")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("INSERT INTO plaid.items DEFAULT VALUES")


def test_finance_observer_and_finance_backup_can_read_alembic_version(role_engine) -> None:
    """migrations/versions/0004: `alembic_version` lives in `public`,
    outside the five application schemas migrations/versions/0002 grants
    across. `finops migration-status` needs finance_observer to read it;
    a full `pg_dump` needs finance_backup to (pg_dump aborts the whole
    dump, not just skips the table, on a permission-denied read)."""
    for role in ("finance_observer", "finance_backup"):
        engine = role_engine(role)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT version_num FROM alembic_version")


def test_finance_backup_can_read_sequences(role_engine) -> None:
    """migrations/versions/0004: pg_dump reads a table's owning sequence
    (last_value/is_called) and aborts the entire dump — not just that one
    table — without SELECT on it. A real failure hit while exercising
    deploy/scripts/backup.sh against the dev container, not a
    hypothetical one; see that migration's docstring."""
    engine = role_engine("finance_backup")
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT last_value FROM plaid.items_id_seq")
        conn.exec_driver_sql("SELECT last_value FROM ops.backup_runs_id_seq")


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


def test_finance_backup_cannot_advance_sequences(role_engine) -> None:
    """`finance_backup` must be able to *read* a sequence's current value
    (pg_dump needs this -- see test_finance_backup_can_read_sequences
    above) without being able to *mutate* it. SELECT on a sequence and
    USAGE/nextval() are separate grants in Postgres; a role that can call
    nextval() could silently perturb an id sequence an ordinary backup
    role has no business touching. Tested, not asserted, per
    docs/security-model.md's "tested, not asserted" invariant."""
    engine = role_engine("finance_backup")
    with (
        engine.connect() as conn,
        pytest.raises(ProgrammingError, match="permission denied"),
    ):
        conn.exec_driver_sql("SELECT nextval('plaid.items_id_seq')")


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
