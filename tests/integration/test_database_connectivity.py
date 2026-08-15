import os

import psycopg
import pytest

pytestmark = pytest.mark.integration


def _dsn() -> str:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://finance_app:devpassword@localhost:5433/finance_dev",
    )
    return url.replace("postgresql+psycopg://", "postgresql://")


def test_can_connect_and_run_a_query() -> None:
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
