"""Explicit production opt-in for the CLI's database connection (ADR-019).

`finance`/`finops` must default to `finance_dev` and reach `finance_prod`
**only** via an explicit environment path — "the default must be incapable
of touching production data at all, not merely configured not to" (ADR-019,
"The CLI defaults to the dev DSN"). This module is the one place that
default gets to change: `FINANCE_ENV_FILE`, an environment variable (never
a CLI flag — a flag is parsed after `db/session.py`'s/`ops/db.py`'s
module-global engines may already have been constructed from the wrong
DSN; an env var is visible before `Settings()` is ever built).

`Settings`'s own `model_validator` (see `config/settings.py`) is the actual
enforcement point — this module only resolves *which file* to load and
answers whether that resolution was explicit, so the validator can refuse a
`finance_prod`-named DSN when it wasn't.
"""

from __future__ import annotations

import os
from pathlib import Path

PRODUCTION_ENV_FILE_VAR = "FINANCE_ENV_FILE"
DEFAULT_ENV_FILE = ".env"


class ProductionAccessRefusedError(RuntimeError):
    """Raised when `FINANCE_ENV_FILE` names a file that cannot be read, or
    (from `Settings`'s validator) when a DSN names `finance_prod` without
    `FINANCE_ENV_FILE` having been explicitly set. Both are refusals, not
    silent fallbacks — a typo'd production path must never quietly resolve
    to the dev defaults, and a stray `finance_prod` DSN in an ordinary
    shell must never quietly reach production."""


def resolve_env_file() -> tuple[str, bool]:
    """`(path, explicit)`. `explicit` is `True` only when `FINANCE_ENV_FILE`
    was actually set in the environment — this is the flag `Settings`'s
    validator checks before allowing a `finance_prod` DSN through.

    A `FINANCE_ENV_FILE` that points at a missing or unreadable file is a
    hard error here, never a silent fallback to `.env`'s dev defaults: an
    operator who mistyped `/opt/finance/.env` must see that immediately,
    not discover a deploy ran against `finance_dev` after the fact."""
    override = os.environ.get(PRODUCTION_ENV_FILE_VAR)
    if override is None:
        return DEFAULT_ENV_FILE, False
    if not Path(override).is_file():
        raise ProductionAccessRefusedError(
            f"{PRODUCTION_ENV_FILE_VAR}={override!r} does not name a readable file. "
            "Refusing to fall back to development defaults — fix the path or unset "
            f"{PRODUCTION_ENV_FILE_VAR}."
        )
    return override, True
