"""Write semantic tools. Every handler here writes only `user.*`/`finance.*`
(handoff §8.1) — none can reach `plaid.*`; that boundary is enforced twice
over: at the database-role level (`finance_agent` has no INSERT/UPDATE/
DELETE grant anywhere in `plaid.*`, see docs/security-model.md invariant 1)
and structurally (no handler in this module imports a `plaid.*` write
path). Each handler validates its inputs and returns a structured
description of the change it applied, per CLAUDE.md's write-tool contract.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.agent.tools._validation import (
    money,
    require_amount,
    require_str,
    require_transaction_id,
)
from finance_app.agent.tools.base import ToolDefinition
from finance_app.agent.tools.errors import ToolInputError
from finance_app.db.models.plaid import Transaction
from finance_app.db.repositories import budgets as budgets_repo
from finance_app.db.repositories import user_annotations

_TRANSACTION_ID_PROPERTY = {
    "transaction_id": {"type": "integer", "description": "The transaction's numeric id."}
}


def _ensure_transaction_exists(session: Session, transaction_id: int) -> None:
    """Read-only check via `finance_agent`'s SELECT grant on `plaid.*` — a
    clear tool-level error beats a raw FK-violation surfacing from the
    database."""
    row = session.execute(
        select(Transaction.id).where(
            Transaction.id == transaction_id, Transaction.removed_at.is_(None)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ToolInputError(f"no transaction with id {transaction_id}")


def set_transaction_category(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    transaction_id = require_transaction_id(args)
    category = require_str(args, "category", max_length=128)
    _ensure_transaction_exists(session, transaction_id)
    override = user_annotations.set_category_override(
        session, transaction_id=transaction_id, category=category, source="agent"
    )
    return {"transaction_id": transaction_id, "category": override.category, "applied": True}


def clear_transaction_category_override(
    session: Session, args: Mapping[str, Any]
) -> dict[str, Any]:
    transaction_id = require_transaction_id(args)
    removed = user_annotations.clear_category_override(session, transaction_id=transaction_id)
    return {"transaction_id": transaction_id, "removed": removed}


def add_transaction_note(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    transaction_id = require_transaction_id(args)
    note = require_str(args, "note", max_length=2000)
    _ensure_transaction_exists(session, transaction_id)
    row = user_annotations.set_note(session, transaction_id=transaction_id, note=note)
    return {"transaction_id": transaction_id, "note": row.note, "applied": True}


def add_transaction_tag(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    transaction_id = require_transaction_id(args)
    tag = require_str(args, "tag", max_length=64)
    _ensure_transaction_exists(session, transaction_id)
    row = user_annotations.add_tag(session, transaction_id=transaction_id, tag=tag)
    return {"transaction_id": transaction_id, "tag": row.tag, "applied": True}


def remove_transaction_tag(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    transaction_id = require_transaction_id(args)
    tag = require_str(args, "tag", max_length=64)
    removed = user_annotations.remove_tag(session, transaction_id=transaction_id, tag=tag)
    return {"transaction_id": transaction_id, "tag": tag, "removed": removed}


def create_budget(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    category = require_str(args, "category", max_length=128)
    monthly_amount = require_amount(args, "monthly_amount")
    if budgets_repo.get_by_category(session, category=category) is not None:
        raise ToolInputError(
            f"a budget for {category!r} already exists; use update_budget to change its amount"
        )
    budget = budgets_repo.create(session, category=category, monthly_amount=monthly_amount)
    return {
        "category": budget.category,
        "monthly_amount": money(budget.monthly_amount),
        "active": budget.active,
    }


def update_budget(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    category = require_str(args, "category", max_length=128)
    monthly_amount = require_amount(args, "monthly_amount")
    budget = budgets_repo.get_by_category(session, category=category)
    if budget is None:
        raise ToolInputError(f"no budget exists for {category!r}; use create_budget first")
    budget = budgets_repo.update_amount(session, budget=budget, monthly_amount=monthly_amount)
    return {
        "category": budget.category,
        "monthly_amount": money(budget.monthly_amount),
        "active": budget.active,
    }


def archive_budget(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    category = require_str(args, "category", max_length=128)
    budget = budgets_repo.get_by_category(session, category=category)
    if budget is None:
        raise ToolInputError(f"no budget exists for {category!r}")
    budget = budgets_repo.archive(session, budget=budget)
    return {"category": budget.category, "active": budget.active}


def update_user_preference(session: Session, args: Mapping[str, Any]) -> dict[str, Any]:
    key = require_str(args, "key", max_length=128)
    value = args.get("value")
    if not isinstance(value, dict):
        raise ToolInputError("'value' must be a JSON object")
    row = user_annotations.set_preference(session, key=key, value=value)
    return {"key": row.key, "value": row.value}


WRITE_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="set_transaction_category",
        description=(
            "Set the effective category for a transaction, overriding Plaid's classification."
        ),
        parameters={
            "type": "object",
            "properties": {
                **_TRANSACTION_ID_PROPERTY,
                "category": {"type": "string", "description": "The category to apply."},
            },
            "required": ["transaction_id", "category"],
            "additionalProperties": False,
        },
        handler=set_transaction_category,
        is_write=True,
    ),
    ToolDefinition(
        name="clear_transaction_category_override",
        description="Remove a category override, restoring Plaid's reported category.",
        parameters={
            "type": "object",
            "properties": {**_TRANSACTION_ID_PROPERTY},
            "required": ["transaction_id"],
            "additionalProperties": False,
        },
        handler=clear_transaction_category_override,
        is_write=True,
    ),
    ToolDefinition(
        name="add_transaction_note",
        description="Attach or replace a free-text note on a transaction.",
        parameters={
            "type": "object",
            "properties": {
                **_TRANSACTION_ID_PROPERTY,
                "note": {"type": "string", "description": "The note text."},
            },
            "required": ["transaction_id", "note"],
            "additionalProperties": False,
        },
        handler=add_transaction_note,
        is_write=True,
    ),
    ToolDefinition(
        name="add_transaction_tag",
        description="Add a short tag to a transaction.",
        parameters={
            "type": "object",
            "properties": {
                **_TRANSACTION_ID_PROPERTY,
                "tag": {"type": "string", "description": "The tag to add."},
            },
            "required": ["transaction_id", "tag"],
            "additionalProperties": False,
        },
        handler=add_transaction_tag,
        is_write=True,
    ),
    ToolDefinition(
        name="remove_transaction_tag",
        description="Remove a tag from a transaction, if present.",
        parameters={
            "type": "object",
            "properties": {
                **_TRANSACTION_ID_PROPERTY,
                "tag": {"type": "string", "description": "The tag to remove."},
            },
            "required": ["transaction_id", "tag"],
            "additionalProperties": False,
        },
        handler=remove_transaction_tag,
        is_write=True,
    ),
    ToolDefinition(
        name="create_budget",
        description="Create a new monthly budget for a category. Fails if one already exists.",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The budget category."},
                "monthly_amount": {
                    "type": "number",
                    "description": "Monthly budget amount, positive.",
                },
            },
            "required": ["category", "monthly_amount"],
            "additionalProperties": False,
        },
        handler=create_budget,
        is_write=True,
    ),
    ToolDefinition(
        name="update_budget",
        description="Change the monthly amount of an existing budget.",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The budget category."},
                "monthly_amount": {
                    "type": "number",
                    "description": "New monthly budget amount, positive.",
                },
            },
            "required": ["category", "monthly_amount"],
            "additionalProperties": False,
        },
        handler=update_budget,
        is_write=True,
    ),
    ToolDefinition(
        name="archive_budget",
        description="Deactivate a budget without deleting its history.",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The budget category."},
            },
            "required": ["category"],
            "additionalProperties": False,
        },
        handler=archive_budget,
        is_write=True,
    ),
    ToolDefinition(
        name="update_user_preference",
        description=(
            "Set a durable user preference (e.g. savings goal, preferred "
            "categories, income cadence)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Preference key, e.g. 'savings_goal'."},
                "value": {"type": "object", "description": "Preference value as a JSON object."},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
        handler=update_user_preference,
        is_write=True,
    ),
]
