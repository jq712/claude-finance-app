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

No new grants are needed: migrations/versions/0002_..._roles_and_grants.py
already grants finance_app SELECT/INSERT/UPDATE/DELETE and finance_backup/
finance_observer SELECT on every table in the `ops` schema via `ALTER
DEFAULT PRIVILEGES FOR ROLE finance_migrator`, so these new tables inherit
the same grants automatically on creation.

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


def downgrade() -> None:
    op.drop_index("ix_releases_release_id", table_name="releases", schema="ops")
    op.drop_table("releases", schema="ops")
    op.drop_table("backup_runs", schema="ops")
