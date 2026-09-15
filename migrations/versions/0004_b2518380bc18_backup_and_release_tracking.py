"""backup and release tracking

Operational state for Milestone 7 (handoff §29, ADR-008, ADR-015):

    ops.backup_runs   one row per backup or restore-verification attempt,
                       read by `finops backup-status`. "A backup that has
                       never been restored is not verified" — this table is
                       what makes that claim checkable rather than assumed.
    ops.releases       one row per production deploy attempt, read by
                       `finops version`/`finops rollback`. Tracks current
                       and previous known-good release per ADR-008 so
                       rollback is one command, not an investigation.

No new grants are needed for these two tables: migrations/versions/
0002_..._roles_and_grants.py already grants finance_app SELECT/INSERT/
UPDATE/DELETE and finance_backup/finance_observer SELECT on every table in
the `ops` schema via `ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator`,
so `ops.backup_runs`/`ops.releases` inherit the same grants automatically
on creation.

This migration does add one grant outside `ops`: Alembic's own
`alembic_version` bookkeeping table lives in the `public` schema (the
Alembic default — this project has never overridden `version_table_schema`),
which migrations/versions/0002 never touched because its SCHEMAS list is
the five application schemas, not `public`. Three roles need to read it
and currently cannot:

  - `finance_backup`, because `pg_dump` aborts the *entire* dump if the
    connecting role lacks SELECT on any table it tries to read, and a
    full-database backup includes `public.alembic_version`;
  - `finance_observer`, because `finops migration-status` (ops/status.py)
    compares the DB's applied version against the repo's head revision,
    and finops's read commands read through finance_observer;
  - `finance_app`, because `ops.health.check_health()` — the
    `finance-health.timer` writer path, run through `db/session.py`'s
    finance_app-bound `session_scope()` (see that module's docstring for
    why it's a different role than the `finops health` CLI command) —
    calls the same `status.aggregate_health`/`migration_status` functions.

None of the three gets anything beyond SELECT on this one bookkeeping table —
migrations remain finance_migrator-only.

Separately, `finops sync-status` (ops/status.py) reports whether the
single Plaid Item is healthy and whether a sync cursor is present — item
status/cursor-presence metadata, not transaction content. `finance_observer`
had no grant anywhere in `plaid` (migration 0002 scoped it to `ops` only),
so this migration adds narrow, table-specific SELECT grants on exactly
`plaid.items` and `plaid.sync_state` — deliberately *not* `plaid.accounts`
or `plaid.transactions`, which hold real financial data and have no
operational-status use case for finance_observer.

Finally, a real gap found while exercising `deploy/scripts/backup.sh`
against the dev container: migration 0002 granted `finance_backup` SELECT
on every *table* in the five application schemas, but not on
*sequences*. `pg_dump` reads each table's owning sequence (for
`last_value`/`is_called`, to make a restored table's `nextval()`
resumption point correct) and aborts the whole dump — not just that one
sequence — if it lacks permission. This migration grants `finance_backup`
SELECT on all existing sequences in every application schema plus `ops`,
and sets `ALTER DEFAULT PRIVILEGES` so sequences created by future
migrations are covered automatically — the same pattern migration 0002
already uses for tables. (`public.alembic_version` has no sequence.)

Revision ID: b2518380bc18
Revises: 715521e125d4
Create Date: 2026-08-17 22:04:28.558043

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2518380bc18"
down_revision: str | Sequence[str] | None = "715521e125d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same five application schemas as migrations/versions/0002.
_APPLICATION_SCHEMAS = ["plaid", "user", "finance", "agent", "ops"]


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        # "backup" | "restore_verification"
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # Path/filename only — never the encryption passphrase or plaintext.
        sa.Column("artifact_path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        # Row counts captured at backup time, used by restore verification
        # to confirm the restored database matches what was dumped.
        sa.Column("source_row_counts", postgresql.JSONB(), nullable=True),
        # For a restore_verification row: which backup_runs.id it verified.
        sa.Column(
            "verifies_backup_id",
            sa.Integer(),
            sa.ForeignKey("ops.backup_runs.id"),
            nullable=True,
        ),
        sa.Column("verification_details", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(length=2000), nullable=True),
        schema="ops",
    )

    op.create_table(
        "releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Immutable Git SHA (or short SHA) identifying the image (ADR-008).
        sa.Column("release_id", sa.String(length=64), nullable=False),
        sa.Column("image_ref", sa.String(length=512), nullable=False),
        # "deploying" | "current" | "previous" | "failed" | "rolled_back"
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "deployed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("health_check_status", sa.String(length=32), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        schema="ops",
    )
    op.create_index(
        "ix_releases_release_id", "releases", ["release_id"], unique=False, schema="ops"
    )

    op.execute("GRANT SELECT ON public.alembic_version TO finance_backup")
    op.execute("GRANT SELECT ON public.alembic_version TO finance_observer")
    op.execute("GRANT SELECT ON public.alembic_version TO finance_app")
    op.execute("GRANT USAGE ON SCHEMA plaid TO finance_observer")
    op.execute("GRANT SELECT ON plaid.items TO finance_observer")
    op.execute("GRANT SELECT ON plaid.sync_state TO finance_observer")

    for schema in _APPLICATION_SCHEMAS:
        op.execute(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO finance_backup')
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "GRANT SELECT ON SEQUENCES TO finance_backup"
        )


def downgrade() -> None:
    for schema in _APPLICATION_SCHEMAS:
        op.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE finance_migrator IN SCHEMA "{schema}" '
            "REVOKE SELECT ON SEQUENCES FROM finance_backup"
        )
        op.execute(f'REVOKE SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" FROM finance_backup')

    op.execute("REVOKE SELECT ON plaid.sync_state FROM finance_observer")
    op.execute("REVOKE SELECT ON plaid.items FROM finance_observer")
    op.execute("REVOKE USAGE ON SCHEMA plaid FROM finance_observer")
    op.execute("REVOKE SELECT ON public.alembic_version FROM finance_app")
    op.execute("REVOKE SELECT ON public.alembic_version FROM finance_observer")
    op.execute("REVOKE SELECT ON public.alembic_version FROM finance_backup")
    op.drop_index("ix_releases_release_id", table_name="releases", schema="ops")
    op.drop_table("releases", schema="ops")
    op.drop_table("backup_runs", schema="ops")
