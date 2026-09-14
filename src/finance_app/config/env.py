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
enforcement point — this module resolves *which file* to load and whether
that file's **own content** (not the merged process environment, which a
plain shell `export FINANCE_ENV=production` could set just as easily,
security-review finding #2) actually declares `FINANCE_ENV=production`.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

PRODUCTION_ENV_FILE_VAR = "FINANCE_ENV_FILE"
DEFAULT_ENV_FILE = ".env"
_PRODUCTION_FINANCE_ENV_VALUE = "production"


class ProductionAccessRefusedError(RuntimeError):
    """Raised when `FINANCE_ENV_FILE` names a file that cannot be read, or
    (from `Settings`'s validator) when a DSN names `finance_prod` without
    the resolved env file's own content declaring `FINANCE_ENV=production`.
    Both are refusals, not silent fallbacks — a typo'd production path must
    never quietly resolve to the dev defaults, and a stray `finance_prod`
    DSN or ambient `FINANCE_ENV=production` in an ordinary shell must never
    quietly reach production."""


def resolve_env_file() -> tuple[str, bool]:
    """`(path, file_declares_production)`.

    `file_declares_production` is `True` only when `FINANCE_ENV_FILE` was
    explicitly set *and* that exact file's own `FINANCE_ENV=` line (parsed
    from the file directly with `dotenv_values`, never from
    `os.environ`/the merged `Settings` a process constructs) equals
    `"production"`. This is deliberately not the same thing as "an
    explicit `FINANCE_ENV_FILE` was set" — security-review finding #1/#2
    on an earlier version of this guard: checking only "was a file
    explicitly named" let `FINANCE_ENV_FILE=<any dev env file>` plus a
    bare shell `export FINANCE_ENV=production` satisfy the opt-in, because
    pydantic-settings' own precedence lets a process env var outrank the
    dotenv file it's meant to gate. Reading the sentinel out of the file's
    own parsed content, and only that, closes both directions: the file
    must both be named explicitly and actually say `production` inside
    itself — nothing set anywhere else in the environment can substitute
    for either half.

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
    file_values = dotenv_values(override)
    file_declares_production = file_values.get("FINANCE_ENV") == _PRODUCTION_FINANCE_ENV_VALUE
    return override, file_declares_production


def production_opt_in() -> bool:
    """The single authoritative "is this a deliberate production
    connection" signal, for callers that only need the boolean (`config/
    settings.py`'s DSN-refusal validator, `cli/finops.py`'s release-root
    guard). Deliberately a *function*, called fresh every time, never a
    value stored on a `pydantic_settings.BaseSettings` field: any such
    field is automatically settable by a same-named environment variable
    unless explicitly fought out of that mapping, and a settable
    `production_opt_in`/`env_file_explicit` field is exactly what
    security-review finding #1/#2 exploited on an earlier version of this
    guard (a bare `export PRODUCTION_OPT_IN=1`/`ENV_FILE_EXPLICIT=1`
    satisfied it with no env file involved at all). A plain function
    cannot be "set" by anything but calling it."""
    _, file_declares_production = resolve_env_file()
    return file_declares_production
