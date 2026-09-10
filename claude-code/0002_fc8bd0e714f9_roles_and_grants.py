"""roles and grants

Least-privilege database roles, per docs/security-model.md invariant 5:

    finance_owner     rarely/never used by normal application flows
    finance_migrator  schema changes during controlled release (bootstrap
                       role of the dev/CI container; already exists)
    finance_app       deterministic application writes, including ingestion
    finance_agent     reads plaid.*; writes only user.*/finance.*/agent.*
    finance_observer  sanitized read-only operational data (ops.*)
    finance_backup    SELECT everywhere, for pg_dump only

The critical invariant this migration enforces mechanically: finance_agent
has no INSERT/UPDATE/DELETE grant anywhere in the plaid schema, so the
runtime financial agent cannot mutate raw Plaid facts even if a prompt
injection or bug tried to make it. Proven by tests/security/test_role_grants.py.

Passwords come from `<ROLE>_DB_PASSWORD` environment variables, falling
back to the same `devpassword` literal already used for finance_migrator
and finance_app in .env.example and deploy/compose.dev.yaml — synthetic,
disposable-container credentials only. Production role passwords are
provisioned out of band as systemd encrypted credentials and are never
read from this repository; see docs/security-model.md invariant 4.

Revision ID: fc8bd0e714f9
Revises: b7f6fdafce87
Create Date: 2026-08-15 02:25:41.225185

"""

import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fc8bd0e714f9"
down_revision: str | Sequence[str] | None = "b7f6fdafce87"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ["finance_owner", "finance_app", "finance_agent", "finance_observer", "finance_backup"]
SCHEMAS = ["plaid", "user", "finance", "agent", "ops"]


def _password_literal(role: str) -> str:
    """A SQL single-quoted string literal for the role's password, read
    from `<ROLE>_DB_PASSWORD` with a synthetic dev-only fallback. Standard
    SQL literal escaping (doubling embedded quotes) — this value comes
    from a trusted operator-controlled environment variable, never from
    request/user input."""
    password = os.environ.get(f"{role.upper()}_DB_PASSWORD", "devpassword")
    return "'" + password.replace("'", "''") + "'"


def upgrade() -> None:
    """Create least-privilege roles and grant per the table above."""
    for role in ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN PASSWORD {_password_literal(role)};
                END IF;
            END
            $$;
            """
        )

    # finance_owner: broad rights, not used by any normal application path.
    for schema in SCHEMAS:
        op.execute(f'GRANT ALL PRIVILEGES ON SCHEMA "{schema}" TO finance_owner')
        op.execute(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA "{schema}" TO finance_owner')
        op.execute(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "{schema}" TO finance_owner')
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            f"GRANT ALL PRIVILEGES ON TABLES TO finance_owner"
        )
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            f"GRANT ALL PRIVILEGES ON SEQUENCES TO finance_owner"
        )

    # finance_app: deterministic application writes across every schema,
    # including Plaid ingestion. No DDL — that stays with finance_migrator.
    for schema in SCHEMAS:
        op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO finance_app')
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
            "TO finance_app"
        )
        op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO finance_app')
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app"
        )
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT USAGE, SELECT ON SEQUENCES TO finance_app"
        )

    # finance_agent: the load-bearing boundary. Read-only on plaid.*;
    # read/write only on user.*, finance.*, agent.*. No grant at all on ops.
    op.execute("GRANT USAGE ON SCHEMA plaid TO finance_agent")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA plaid TO finance_agent")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA plaid "
        "GRANT SELECT ON TABLES TO finance_agent"
    )
    for schema in ("user", "finance", "agent"):
        op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO finance_agent')
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
            "TO finance_agent"
        )
        op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO finance_agent')
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_agent"
        )
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT USAGE, SELECT ON SEQUENCES TO finance_agent"
        )

    # finance_observer: sanitized operational data only.
    op.execute("GRANT USAGE ON SCHEMA ops TO finance_observer")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ops TO finance_observer")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA ops "
        "GRANT SELECT ON TABLES TO finance_observer"
    )

    # finance_backup: SELECT everywhere, nothing else — pg_dump needs to
    # read every table; it never needs to write one.
    for schema in SCHEMAS:
        op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO finance_backup')
        op.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO finance_backup')
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT SELECT ON TABLES TO finance_backup"
        )


def downgrade() -> None:
    """Drop the least-privilege roles. Grants disappear with them; default
    privilege entries owned by finance_migrator are dropped explicitly."""
    for schema in SCHEMAS:
        for role in ROLES:
            op.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
                f"REVOKE ALL ON TABLES FROM {role}"
            )
            op.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
                f"REVOKE ALL ON SEQUENCES FROM {role}"
            )
    for role in ROLES:
        op.execute(f"DROP OWNED BY {role}")
        op.execute(f"DROP ROLE IF EXISTS {role}")
