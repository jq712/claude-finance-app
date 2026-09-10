"""release rollback safety

QA (Milestone 7 production-deployment review), findings QA-2/QA-3/QA-4:

    ops.releases.replaces_release_id   the release_id that was `current`
                                         at the moment a given deploy
                                         attempt started, captured by
                                         `start_deploy` — lets a failed
                                         deploy's auto-rollback target the
                                         release it actually replaced,
                                         instead of inferring "two steps
                                         back" from a generic status query
                                         (src/finance_app/ops/release.py).

    ux_releases_current_previous       a partial unique index on
                                         `status` for rows where
                                         `status IN ('current', 'previous')`
                                         — at most one `current` row and
                                         at most one `previous` row can
                                         exist at a time, so two racing
                                         `finops deploy` invocations can no
                                         longer both commit `current`.

Never rewrites migration 0004 (CLAUDE.md) — this adds a column and an
index to the table it created.

Revision ID: a1c3e9f4d2b7
Revises: b2518380bc18
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c3e9f4d2b7"
down_revision: str | Sequence[str] | None = "b2518380bc18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "releases",
        sa.Column("replaces_release_id", sa.String(length=64), nullable=True),
        schema="ops",
    )
    op.create_index(
        "ux_releases_current_previous",
        "releases",
        ["status"],
        unique=True,
        schema="ops",
        postgresql_where=sa.text("status IN ('current', 'previous')"),
    )


def downgrade() -> None:
    op.drop_index("ux_releases_current_previous", table_name="releases", schema="ops")
    op.drop_column("releases", "replaces_release_id", schema="ops")
