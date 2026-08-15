"""Effective-category resolution — the one place spending/income/cashflow
analytics decide what a transaction *is*. This is the handoff's core idea
made concrete:

    Plaid fact + user/agent interpretation = effective financial view

A transaction's raw Plaid category (`personal_finance_category` if Plaid
supplied it, else the legacy `plaid_category` list) never changes. A user
or agent override in `user.transaction_category_overrides` — read, never
written, by this module — supplies the interpretation layered on top.

Transfers between the user's own accounts and income deposits are
excluded from spending totals (handoff §30: "transfers not double-counted
as spending/income"). Both classifications are read from the *raw* Plaid
category by default, but an override always wins in *either* direction:
if the user corrects a transaction Plaid classified as an internal
transfer to a real spending category, analytics must honor that
correction rather than the original (possibly wrong) Plaid
classification — and symmetrically, if the user explicitly labels a
transaction "Transfer" or "Income" (case-insensitively; overrides come
from free text, an agent tool, or a CLI flag with no enforced casing),
that must exclude it from spending exactly as a raw Plaid transfer/income
classification would. An override is compared against these sentinel
category names case-insensitively for this reason.
"""

from dataclasses import dataclass

_TRANSFER_LEGACY_CATEGORIES = {"Transfer"}
_TRANSFER_PFC_PRIMARIES = {"TRANSFER_IN", "TRANSFER_OUT"}
_TRANSFER_OVERRIDE_NAMES = {c.casefold() for c in _TRANSFER_LEGACY_CATEGORIES}

_INCOME_LEGACY_CATEGORIES = {"Payroll", "Income"}
_INCOME_PFC_PRIMARIES = {
    "INCOME_WAGES",
    "INCOME_DIVIDENDS",
    "INCOME_INTEREST_EARNED",
    "INCOME_RETIREMENT_PENSION",
    "INCOME_TAX_REFUND",
    "INCOME_UNEMPLOYMENT",
    "INCOME_OTHER",
}
_INCOME_OVERRIDE_NAMES = {c.casefold() for c in _INCOME_LEGACY_CATEGORIES}

UNCATEGORIZED = "Uncategorized"


@dataclass(frozen=True, slots=True)
class TransactionClassification:
    effective_category: str
    is_transfer: bool
    is_income: bool


def _raw_label(personal_finance_category: dict | None, plaid_category: list | None) -> str | None:
    if personal_finance_category is not None:
        label = personal_finance_category.get("detailed") or personal_finance_category.get(
            "primary"
        )
        if label:
            return label.replace("_", " ").title()
    if plaid_category:
        # Plaid's legacy taxonomy orders general -> specific; the last
        # element is the most specific label ("Restaurants", not "Food and
        # Drink"), matching how the handoff's own examples name categories.
        return plaid_category[-1]
    return None


def _pfc_primary(personal_finance_category: dict | None) -> str | None:
    if personal_finance_category is None:
        return None
    primary = personal_finance_category.get("primary")
    return primary if isinstance(primary, str) else None


def classify(
    *,
    override_category: str | None,
    personal_finance_category: dict | None,
    plaid_category: list | None,
) -> TransactionClassification:
    pfc_primary = _pfc_primary(personal_finance_category)
    legacy_top_label = plaid_category[0] if plaid_category else None

    if override_category is not None:
        override_normalized = override_category.strip().casefold()
        return TransactionClassification(
            effective_category=override_category,
            is_transfer=override_normalized in _TRANSFER_OVERRIDE_NAMES,
            is_income=override_normalized in _INCOME_OVERRIDE_NAMES,
        )

    raw_label = _raw_label(personal_finance_category, plaid_category)
    is_transfer = (
        pfc_primary in _TRANSFER_PFC_PRIMARIES or legacy_top_label in _TRANSFER_LEGACY_CATEGORIES
    )
    is_income = (
        pfc_primary in _INCOME_PFC_PRIMARIES or legacy_top_label in _INCOME_LEGACY_CATEGORIES
    )
    return TransactionClassification(
        effective_category=raw_label or UNCATEGORIZED,
        is_transfer=is_transfer,
        is_income=is_income,
    )
