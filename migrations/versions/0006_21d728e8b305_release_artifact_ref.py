"""release artifact_ref rename

Milestone 7, ADR-019 (bare-metal deploy, no Docker). `ops.releases.image_ref`
held a GHCR image reference (`ghcr.io/<owner>/<repo>:<sha>`) under the
superseded Docker Compose deploy model (ADR-016). Under ADR-019 there is no
image — the column now holds a release directory path
(`/opt/finance/releases/<sha>`) instead. Renamed to the substrate-neutral
`artifact_ref` rather than something filesystem-specific like
`release_path`, so it does not need renaming again if the release
mechanism changes a third time. Not `release_ref` — too easily confused
with the existing `release_id` column (the immutable Git SHA), which this
migration does not touch.

Data-loss assessment: **none.** `ALTER TABLE ... RENAME COLUMN` is
catalog-only in PostgreSQL — no table rewrite, no data movement, type and
`NOT NULL` are preserved exactly. Safe as a straight rename (rather than
an additive-then-contract migration) *only* because there is no deployed
reader of this column yet: `/opt/finance` and the `finance_prod` database
do not exist (ADR-019's own "Implementation status" table, and
`docs/deployment.md`, both confirm this as of the date of this migration).
This straight-rename approach must not be repeated once a real production
release has been recorded through this column — Milestone 7's implementation
status is why this window is still open, not a precedent for later renames.

No index or constraint references `image_ref`: `migrations/versions/
0004_..._backup_and_release_tracking.py` indexes only `release_id`
(`ix_releases_release_id`), and `0005_..._release_rollback_safety.py`'s
partial unique index is on `status`. Grants on `ops.releases` are
table/schema-level (`0004`), unaffected by a column rename. Neither 0004
nor 0005 is edited (CLAUDE.md: never rewrite an already-applied migration).

Revision ID: 21d728e8b305
Revises: a1c3e9f4d2b7
Create Date: 2026-09-13 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "21d728e8b305"
down_revision: str | Sequence[str] | None = "a1c3e9f4d2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("releases", "image_ref", new_column_name="artifact_ref", schema="ops")


def downgrade() -> None:
    op.alter_column("releases", "artifact_ref", new_column_name="image_ref", schema="ops")
