import pytest
from sqlalchemy import inspect

from tests.conftest import role_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def migrator_engine(role_engine):
    return role_engine("finance_migrator")


def test_all_schemas_exist(migrator_engine) -> None:
    with migrator_engine.connect() as conn:
        inspector = inspect(conn)
        schemas = set(inspector.get_schema_names())
    assert {"plaid", "user", "finance", "agent", "ops"} <= schemas


def test_core_tables_exist_in_expected_schemas(migrator_engine) -> None:
    with migrator_engine.connect() as conn:
        inspector = inspect(conn)
        expected = {
            "plaid": {"items", "accounts", "transactions", "sync_state"},
            "user": {
                "transaction_category_overrides",
                "transaction_tags",
                "transaction_notes",
                "preferences",
            },
            "finance": {"budgets"},
            "agent": {"conversations", "messages", "analysis_runs", "tool_calls"},
            "ops": {"job_runs", "sync_runs", "errors", "backup_runs", "releases"},
        }
        for schema, tables in expected.items():
            actual = set(inspector.get_table_names(schema=schema))
            assert tables <= actual, f"schema {schema} missing {tables - actual}"


def test_alembic_is_at_a_known_head(migrator_engine) -> None:
    with migrator_engine.connect() as conn:
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == "b2518380bc18"


def test_migrator_dsn_matches_the_role_that_ran_migrations() -> None:
    # Sanity check on the test helper itself: this is the same role
    # deploy/compose.dev.yaml and CI configure as the container bootstrap
    # user, i.e. the one that actually ran `alembic upgrade head`.
    assert role_dsn("finance_migrator").startswith("postgresql+psycopg://finance_migrator:")
