"""Adversarial regression tests for ADR-019's production-access guard and
release-identity derivation (QA round 6, Milestone 7 — ADR-019,
docs/security-model.md).

Two mechanisms were attacked here, both introduced by commit b8c6cbb and
both fully exercisable without a database or a release tree. All four
defects below (QA-53..56) were confirmed against that commit and are now
fixed — every test in this file asserts the *fixed* behavior directly
(no `xfail` markers remain; per this project's convention, a marker is
deleted once its defect is fixed, never the test):

`cli/finops.py:_require_release_root_authority` — the guard that stops an
operator from writing deploy bookkeeping to `finance_dev` while the
symlink/`systemctl` operations act on the real `/opt/finance` tree.

`ops/identity.py:installed_release_root` — the "non-forgeable" release
identity carrying QA-37's invariant forward. It derives the release
directory positionally (`sys.argv[0]` resolved, three parents up) and
then reads a `RELEASE_ID` file from wherever that lands.

* QA-53 (fixed) — the guard used to compare `release_root ==
  "/opt/finance"` as a string. `--release-root /opt/finance/` (what shell
  tab-completion produces for a directory), `/opt/finance//`,
  `/opt/finance/.` and `/opt/./finance` all address the identical
  production tree and all used to skip the guard entirely. Fixed by
  comparing resolved paths.
* QA-54 (fixed) — the same guard used to check only whether
  `FINANCE_ENV_FILE` was explicitly set, never whether the named file's
  own content actually declared `FINANCE_ENV=production`. Fixed by
  `config.env.production_opt_in()` reading the resolved file's own parsed
  content directly.
* QA-55 (fixed) — nothing used to guard the inverse split: a `Settings`
  legitimately built for production (a `finance_prod` DSN) combined with
  an arbitrary `--release-root` used to write release bookkeeping into
  `finance_prod` while touching a scratch directory. Fixed: the guard now
  also refuses when opted into production but the release root does
  *not* resolve to the production default.
* QA-56 (fixed) — `installed_release_root()` used to report a directory
  outside the releases tree — and `installed_release_id()` then reported
  an identity read from it — whenever a release's `.venv` was a symlink,
  or `sys.argv[0]` was a bare command name resolved via `$PATH`. Fixed by
  verifying the derived directory's parent is literally named `releases`
  before trusting it, and by refusing to resolve a bare command name
  (no path separator in `sys.argv[0]`) against an arbitrary `cwd` at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import typer

import finance_app.cli.finops as finops_module
from finance_app.config.settings import Settings, get_settings
from finance_app.ops import identity
from finance_app.ops.host import DEFAULT_RELEASE_ROOT, release_dir


def _settings_for(env_file: Path | None, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`Settings` as a real `finops` invocation gets them: built by
    `get_settings()` from whatever `FINANCE_ENV_FILE` names, or from the
    dev defaults when it is unset.

    Deliberately goes through `get_settings()` rather than constructing
    `Settings(<internal opt-in flag>=...)` directly. The name and
    semantics of that internal flag are exactly what a fix to QA-54 is
    likely to change, and a test that sets it by keyword would silently
    stop exercising anything (`model_config` uses `extra="ignore"`, so a
    renamed field is dropped without error) while still appearing to
    assert something. The environment variable is the operator-facing
    contract and cannot be refactored out from under this test."""
    if env_file is None:
        monkeypatch.delenv("FINANCE_ENV_FILE", raising=False)
    else:
        monkeypatch.setenv("FINANCE_ENV_FILE", str(env_file))
    return get_settings()


def _guard_allows(release_root: str) -> bool:
    """Whether `_require_release_root_authority` lets this combination
    proceed. It signals refusal with `typer.Exit`.

    Takes no `Settings` argument: the fixed guard reads
    `config.env.production_opt_in()` fresh on every call rather than a
    field threaded through from a `Settings` instance (that field was
    itself the QA-1/QA-2-shaped forgeability bug this whole mechanism
    exists to avoid repeating — see `config/env.py:production_opt_in`'s
    docstring). `_settings_for`'s `monkeypatch.setenv`/`delenv` calls are
    what this guard actually observes; the `Settings` object a test also
    builds is for its own DSN/`finance_env` assertions, not for feeding
    into this function."""
    try:
        finops_module._require_release_root_authority(release_root)
    except typer.Exit:
        return False
    return True


# ---------------------------------------------------------------------------
# QA-53 — the guard is a string comparison against one spelling of a path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("/opt/finance/", id="trailing-slash"),
        pytest.param("/opt/finance//", id="double-trailing-slash"),
        pytest.param("/opt/finance/.", id="trailing-dot"),
        pytest.param("/opt/./finance", id="interior-dot"),
    ],
)
def test_production_release_root_guard_is_not_bypassed_by_path_spelling(
    spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_require_release_root_authority` exists to prevent one specific
    split: deploy bookkeeping written to `finance_dev` (because `Settings`
    fell back to dev defaults) while `repoint_current` and `systemctl
    restart` act on the real production release tree. Its own docstring
    names that as "a bookkeeping/filesystem split with no error at all".

    QA-53 (fixed): the guard used to detect the condition with
    `release_root == DEFAULT_RELEASE_ROOT` — a literal string comparison.
    Every spelling parametrized here resolves to the identical directory,
    and `ops/host.py` uses `Path(...)` throughout, so
    `release_dir`/`current_link`/`repoint_current` all target
    `/opt/finance/...` regardless of which spelling was passed — only the
    guard used to see a difference. Fixed by comparing
    `Path(release_root).resolve()` against `Path(DEFAULT_RELEASE_ROOT).resolve()`.

    A trailing slash is not an adversarial input: it is what shell
    tab-completion appends to a directory name, so
    `finops deploy <sha> --release-root /opt/finance/` is the *likely*
    spelling for an operator who typed the path rather than copying it out
    of the runbook.
    """
    _settings_for(None, monkeypatch)
    # Precondition: the guard does fire for the canonical spelling, so a
    # failure below is a bypass and not the guard being absent entirely.
    assert not _guard_allows(DEFAULT_RELEASE_ROOT)
    # Precondition: this spelling really does address the production tree.
    assert release_dir(spelling, "abc1234") == release_dir(DEFAULT_RELEASE_ROOT, "abc1234")

    assert not _guard_allows(spelling), (
        f"--release-root {spelling!r} addresses {release_dir(spelling, 'abc1234')} — the "
        "real production release tree — but skipped the guard, so deploy bookkeeping would "
        "be written to finance_dev while the `current` symlink and systemd units of "
        "production were changed"
    )


# ---------------------------------------------------------------------------
# QA-54 — the guard checks a different condition than its docstring.
# ---------------------------------------------------------------------------


def test_production_release_root_guard_requires_a_production_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's docstring states its condition precisely: refuse to
    operate against the real production release root unless this
    process's own settings were built from an explicitly-set
    `FINANCE_ENV_FILE` whose own content declares `FINANCE_ENV=production`.

    QA-54 (fixed): the guard used to check only "was `FINANCE_ENV_FILE`
    explicitly set", never whether the named file's own content actually
    declared `FINANCE_ENV=production` — `Settings`'s own DSN validator
    only ever *refuses a `finance_prod` DSN*, so a dev DSN passed through
    with no backstop. `FINANCE_ENV_FILE=/home/someone/dev.env finops
    deploy <sha> --release-root /opt/finance` used to be permitted, with
    `database_url`/`observer_database_url` still pointing at `finance_dev`
    — the exact split the guard's docstring says it prevents. Fixed by
    `config.env.production_opt_in()` reading the resolved file's own
    parsed `FINANCE_ENV=` line directly, never a `Settings` field.

    An operator hitting this has a plausible reason to have
    `FINANCE_ENV_FILE` set at all: it is the documented mechanism for
    pointing the CLI at *an* env file, and a stale or wrong value is
    precisely what `config/env.py`'s own docstring warns about ("an
    operator who mistyped /opt/finance/.env must see that immediately").
    """
    dev_env_file = tmp_path / "dev.env"
    dev_env_file.write_text("FINANCE_ENV=development\n", encoding="utf-8")
    settings = _settings_for(dev_env_file, monkeypatch)

    # Preconditions: this really is a non-production configuration whose
    # bookkeeping would land in finance_dev.
    assert settings.finance_env != "production"
    assert "finance_dev" in settings.database_url.get_secret_value()
    assert "finance_dev" in settings.observer_database_url.get_secret_value()

    assert not _guard_allows(DEFAULT_RELEASE_ROOT), (
        "operating on /opt/finance was permitted with FINANCE_ENV_FILE pointing at a "
        f"development env file (finance_env={settings.finance_env!r}); deploy bookkeeping "
        "would be written to finance_dev while production's `current` symlink and systemd "
        "units were changed"
    )


# ---------------------------------------------------------------------------
# QA-55 — nothing guards the inverse split (prod database, scratch tree).
# ---------------------------------------------------------------------------


def test_production_database_refuses_a_non_production_release_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-55 (fixed): `_require_release_root_authority` used to only ever
    inspect the case "production path, non-production settings". The
    mirror case was unguarded: a `Settings` built from the real production
    env file (so `session_scope`/`observer_session_scope` connect to
    `finance_prod`) combined with `--release-root /tmp/anything`.

    That is not harmless. `finops deploy` in that shape would:

    * `start_deploy` a row into **`finance_prod`**'s `ops.releases`;
    * probe and repoint a `current` symlink inside the scratch tree;
    * on success, `mark_healthy` — which demotes production's real
      `current` row to `previous` and promotes a release that is not
      running anywhere in production;
    * on failure, `_do_rollback` — which resolves and promotes rows in
      production's bookkeeping based on a scratch directory's health.

    After that, production's `ops.releases` describes a release topology
    that has nothing to do with `/opt/finance`, `finops restart` refuses
    on a disagreement it cannot explain, and `finops rollback`'s target is
    whatever the corrupted bookkeeping now says. The `--release-root`
    escape hatch that makes the deploy path unit-testable is the same flag
    that reaches this.

    A production-named DSN and a non-production release root should not
    both be accepted by the same invocation.
    """
    prod_env_file = tmp_path / "production.env"
    prod_env_file.write_text(
        "FINANCE_ENV=production\n"
        "DATABASE_URL=postgresql+psycopg://finance_app:x@localhost:5432/finance_prod\n"
        "OBSERVER_DATABASE_URL="
        "postgresql+psycopg://finance_observer:x@localhost:5432/finance_prod\n",
        encoding="utf-8",
    )
    settings = _settings_for(prod_env_file, monkeypatch)

    # Preconditions: this Settings really does talk to production.
    assert settings.finance_env == "production"
    assert "finance_prod" in settings.database_url.get_secret_value()

    scratch_root = str(tmp_path / "scratch-release-root")
    assert scratch_root != DEFAULT_RELEASE_ROOT

    assert not _guard_allows(scratch_root), (
        f"a Settings connected to finance_prod was allowed to operate on {scratch_root!r} — "
        "release bookkeeping (including mark_healthy's demotion of production's real "
        "`current` row) would be written to production's database while the symlink and "
        "systemd operations acted on a directory production never runs from"
    )


# ---------------------------------------------------------------------------
# QA-56 — positional release-root derivation, no validation of the result.
# ---------------------------------------------------------------------------


def _build_release_tree(root: Path, release_id: str, *, identity_value: str | None = None) -> Path:
    release_path = root / "releases" / release_id
    (release_path / ".venv" / "bin").mkdir(parents=True)
    (release_path / ".venv" / "bin" / "finance").touch()
    (release_path / "RELEASE_ID").write_text(
        (identity_value if identity_value is not None else release_id) + "\n", encoding="utf-8"
    )
    return release_path


def test_installed_release_id_fails_closed_when_the_venv_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ops/identity.py`'s module docstring is explicit about what this
    derivation is for: "`current` can be repointed at the wrong release
    directory ... and a release directory's *contents* can disagree with
    its *name*", and "resolving through symlinks is what makes this answer
    'which release is *actually* running' rather than 'which path was
    named on the command line'".

    `installed_release_root()` implements that as
    `Path(sys.argv[0]).resolve().parents[2]` — correct only if
    `sys.argv[0]` is literally `<release>/.venv/bin/<name>` with no
    symlink anywhere *inside* that suffix. Resolving the whole path is
    what breaks it: if `.venv` is a symlink (a shared virtualenv, a
    hand-relinked venv after a failed `uv sync`, a release copied with
    `cp -a` from a tree that had one), the resolved path is
    `<somewhere-else>/bin/finance` and `parents[2]` is a directory that is
    not a release at all.

    Constructed here: `releases/def5678/.venv -> <root>/shared-venv`.
    `installed_release_root()` then returns `<root>` and
    `installed_release_id()` reads `<root>/RELEASE_ID`, reporting
    `deadbee` — a syntactically valid, entirely wrong SHA — instead of
    `def5678` or `None`.

    Two problems, either of which is enough:

    * the reported identity is wrong rather than absent, so it does not
      fail closed the way `read_release_identity`'s docstring promises
      ("Every failure mode returns `None` rather than raising — a missing
      or malformed identity file must fail the deploy/probe gate closed");
    * it is read from a file *outside* `releases/`, i.e. one not placed
      there by `git archive export-subst`, which is the entire basis for
      calling this identity non-forgeable. A `RELEASE_ID` at the release
      root is not an archived artifact — it is whatever is on disk.

    `installed_release_root()` should verify its result before trusting
    it: the derived directory's parent must be named `releases`, and its
    name must match the directory it was derived from — otherwise return
    nothing and let the gate fail closed.
    """
    root = tmp_path / "opt" / "finance"
    shared_venv_bin = root / "shared-venv" / "bin"
    shared_venv_bin.mkdir(parents=True)
    (shared_venv_bin / "finance").touch()
    # A RELEASE_ID sitting at the release root rather than inside a
    # release — not an archived artifact, so it must never be read as one.
    (root / "RELEASE_ID").write_text("deadbee\n", encoding="utf-8")

    release_path = root / "releases" / "def5678"
    release_path.mkdir(parents=True)
    (release_path / "RELEASE_ID").write_text("def5678\n", encoding="utf-8")
    (release_path / ".venv").symlink_to(root / "shared-venv", target_is_directory=True)

    monkeypatch.setattr(sys, "argv", [str(release_path / ".venv" / "bin" / "finance")])

    reported = identity.installed_release_id()
    assert reported in (None, "def5678"), (
        f"installed_release_id() reported {reported!r} for a process launched from "
        f"{release_path} — a SHA read out of {root / 'RELEASE_ID'}, which is outside the "
        "releases tree and was never produced by `git archive export-subst`; "
        f"installed_release_root() returned {identity.installed_release_root()}"
    )


def test_installed_release_root_is_correct_for_the_normal_invocation_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for QA-56: the shape `ops/host.py:run_release` actually
    constructs, and the `via_current=True` shape, must keep working
    whatever fix lands. Also pins the relative-`argv[0]` case, which is
    correct today and must not regress."""
    root = tmp_path / "opt" / "finance"
    release_path = _build_release_tree(root, "abc1234")
    (root / "current").symlink_to(Path("releases") / "abc1234", target_is_directory=True)

    monkeypatch.setattr(sys, "argv", [str(release_path / ".venv" / "bin" / "finance")])
    assert identity.installed_release_root() == release_path
    assert identity.installed_release_id() == "abc1234"

    # Through `current` — the post-restart re-probe's invocation shape.
    monkeypatch.setattr(sys, "argv", [str(root / "current" / ".venv" / "bin" / "finance")])
    assert identity.installed_release_root() == release_path
    assert identity.installed_release_id() == "abc1234"

    # A relative argv[0] resolved against the process's cwd.
    monkeypatch.chdir(release_path / ".venv" / "bin")
    monkeypatch.setattr(sys, "argv", ["./finance"])
    assert identity.installed_release_id() == "abc1234"


def test_installed_release_id_is_none_for_a_bare_argv0_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sharper half of QA-56, and the one that actually contradicts the
    word "non-forgeable".

    A bare `finance` on `$PATH` — a systemd unit whose `ExecStart` is not
    an absolute path, an operator's interactive shell, anything invoked
    through a wrapper script — gives `sys.argv[0] == "finance"`.
    `Path("finance").resolve()` is then `<cwd>/finance`, and `parents[2]`
    is two directories above the process's working directory. No release
    tree is involved at all.

    `read_release_identity` is then handed that directory and happily
    reads a `RELEASE_ID` from it. So the identity `ops/selfcheck.py`
    reports — the value `probe_release`'s `wrong_release` gate compares
    against, the one thing in this design that is supposed to be fixed
    before `finops deploy` runs and outside `finops`'s control — is
    selected by the cwd of the process being checked. Anyone who can
    choose that cwd, or drop a file two levels above it, chooses the
    answer. That is the definition of forgeable, and it is the exact
    property `ops/identity.py`'s docstring claims this mechanism has over
    the QA-37 environment-variable comparison it replaced.

    Asserted as `is None`: a derivation that cannot identify a release
    must say so, which is what `read_release_identity`'s "fail the
    deploy/probe gate closed" contract requires. Validating that the
    derived directory is really `<root>/releases/<name>` closes both this
    and the symlinked-`.venv` case above."""
    workdir = tmp_path / "a" / "b" / "c"
    workdir.mkdir(parents=True)
    # An unrelated RELEASE_ID two levels up from the cwd, where the
    # positional derivation lands.
    (tmp_path / "a" / "RELEASE_ID").write_text("f00ba12\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(sys, "argv", ["finance"])

    assert identity.installed_release_id() is None, (
        "a bare `finance` on $PATH produced an identity from an unrelated directory: "
        f"{identity.installed_release_root()}"
    )
