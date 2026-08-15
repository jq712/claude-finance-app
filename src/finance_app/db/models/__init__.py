from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base. All schemas/tables are registered on this
    metadata so Alembic autogenerate sees the whole model."""

    metadata_naming_convention = NAMING_CONVENTION


Base.metadata.naming_convention = NAMING_CONVENTION

# Import order registers every table on Base.metadata for Alembic autogenerate.
from finance_app.db.models import agent, finance, ops, plaid, user  # noqa: E402,F401

__all__ = ["Base"]
