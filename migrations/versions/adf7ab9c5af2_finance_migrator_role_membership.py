"""finance_migrator role membership

Fixes a `DROP OWNED BY` permission failure discovered by
tests/integration/test_migration_reversibility.py: `alembic downgrade
b7f6fdafce87` (unwinding past 0002_fc8bd0e714f9_roles_and_grants) exits
non-zero because that migration's downgrade() runs `DROP OWNED BY
<role>` for each of finance_owner/finance_app/finance_agent/
finance_observer/finance_backup in turn, and PostgreSQL requires the
executing role to be a direct or indirect member of the target role (or
a superuser) to do that. `finance_migrator` has CREATEROLE — enough to
CREATE ROLE each of the five in 0002's upgrade() — but CREATEROLE alone
does not confer membership. No migration or bootstrap script anywhere in
this repository has ever granted that membership, so the very first
role in the loop (finance_owner) fails with a Postgres permission-denied
error and the downgrade aborts before reaching finance_app/
finance_agent/finance_observer/finance_backup at all.

This migration grants finance_migrator plain membership (no ADMIN
OPTION — it never needs to hand these roles to anyone else, only to
exercise DROP OWNED BY on its own behalf) in all five roles.

downgrade() is deliberately a no-op, not a silent oversight. Alembic
always runs downgrades newest-revision-first. If downgrade() here
issued the symmetric `REVOKE <role> FROM finance_migrator`, then
`alembic downgrade b7f6fdafce87` would run *this* migration's
downgrade() before it reaches 0002's downgrade() — stripping the very
membership 0002's `DROP OWNED BY` loop needs, reintroducing the exact
bug this migration exists to fix. Leaving it a no-op is safe: 0002's
downgrade() ends with `DROP ROLE IF EXISTS <role>` for all five roles,
and dropping a role destroys every membership grant naming it (as
group or as member) automatically — so by the time the downgrade chain
actually reaches and clears 0002, there is nothing left for this
migration's downgrade to revoke regardless of whether it ran.

`GRANT role TO role` is naturally idempotent in Postgres (re-granting
emits a NOTICE, not an ERROR) — no IF-NOT-EXISTS guard is needed,
unlike 0002_fc8bd0e714f9's `CREATE ROLE ... DO $$` pattern.

Revision ID: adf7ab9c5af2
Revises: 21d728e8b305
Create Date: 2026-09-14 20:28:32.566793

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "adf7ab9c5af2"
down_revision: str | Sequence[str] | None = "21d728e8b305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ["finance_owner", "finance_app", "finance_agent", "finance_observer", "finance_backup"]


def upgrade() -> None:
    """Grant finance_migrator membership in all five managed roles."""
    for role in ROLES:
        op.execute(f"GRANT {role} TO finance_migrator")


def downgrade() -> None:
    """Deliberately a no-op — see module docstring."""
