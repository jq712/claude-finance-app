"""finance_migrator role privilege inheritance

Fixes a `DROP OWNED BY` permission failure discovered by
tests/integration/test_migration_reversibility.py: `alembic downgrade
b7f6fdafce87` (unwinding past 0002_fc8bd0e714f9_roles_and_grants) exits
non-zero with "permission denied to drop objects owned by it" because
that migration's downgrade() runs `DROP OWNED BY <role>` for each of
finance_owner/finance_app/finance_agent/finance_observer/finance_backup.
PostgreSQL's DROP OWNED BY requires the executing role to have the
INHERIT privilege in the target role (not just membership; this is the
has_privs_of_role() check, not mere membership). On PG 16+, CREATEROLE
automatically grants membership to the creator, but without INHERIT.
finance_migrator created the five roles in 0002's upgrade() and
auto-received membership without INHERIT, so the first role in the
downgrade loop (finance_owner) fails with a permission error.

This migration explicitly grants finance_migrator with INHERIT TRUE
(enabling INHERIT privilege in all five roles) and SET FALSE (privilege
scope is not needed). The explicit form is immune to drift in
rolinherit or createrole_self_grant cluster settings — and unlike plain
`GRANT role TO role` (which inherits the grantee's rolinherit), this
form is minimal and specific.

downgrade() is deliberately a no-op, not a silent oversight. Alembic
always runs downgrades newest-revision-first. If downgrade() here
issued the symmetric `REVOKE`, then `alembic downgrade b7f6fdafce87`
would run *this* migration's downgrade() before 0002's downgrade() —
stripping the INHERIT that 0002's `DROP OWNED BY` loop needs,
reintroducing the exact bug. Leaving it a no-op is safe: 0002's
downgrade() ends with `DROP ROLE IF EXISTS <role>` for all five roles,
which destroys every membership grant naming them, INHERIT or not.

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
    """Grant finance_migrator INHERIT privilege in all five managed roles."""
    for role in ROLES:
        op.execute(f"GRANT {role} TO finance_migrator WITH INHERIT TRUE, SET FALSE")


def downgrade() -> None:
    """Deliberately a no-op — see module docstring."""
