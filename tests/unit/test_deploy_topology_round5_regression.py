"""Adversarial regression tests for the *round-4 fixes* (QA-34..QA-42) and
the security-review hardening that followed them (QA round 5, Milestone 7 —
handoff §19/§20, ADR-008, ADR-016).

Round 4 fixed nine defects; round 5 attacked each fix directly. Three of
them stop one variant of the failure while leaving a neighbouring variant
open, and one of the *hardening* changes reopened the defect it was
hardening:

* QA-43 — `with-production-env.sh`'s NUL guard was rewritten from a
  pipeline (`od | tr | grep -q ' 00 '`) into a command substitution plus a
  `case` on the same pattern. `$(...)` strips trailing newlines, so the
  trailing space the `' 00 '` pattern depends on is gone for the *last*
  byte of the file: a credential ending in a NUL byte is exported
  truncated, exit 0, which is exactly QA-34.
* QA-44 — QA-42's markup fix wrapped one of five `console.print` call
  sites in `Text`. `finops restart`, `finops rollback`, and `deploy`'s own
  `RollbackTargetUnhealthyError` handler still interpolate the probe
  payload into a Rich markup string and crash with `MarkupError`.
* QA-45 (Docker-era; superseded under ADR-019) — nothing pinned the
  property QA-37's whole `wrong_image` fix rested on: that
  `deploy/compose.yaml` never let `IMAGE_RELEASE_ID` be set at
  container-start time. Under the bare-metal model there is no image and
  no such variable at all — this invariant is now covered by an
  *executable* end-to-end test in `test_deploy_topology_baremetal.py`
  (a real `git archive` plus a real forged-`sys.argv[0]` attempt) rather
  than a static grep, which is strictly stronger. See that file's QA-45
  section header for the full account of what moved and why.
* QA-46 — QA-35's leading/trailing-whitespace rejection covers space and
  tab only, while the script's own required-credential check uses
  `tr -d '[:space:]'`; a trailing form feed or vertical tab is exported
  verbatim.

Defect tests are `xfail(strict=True)`, per the convention in
`tests/unit/test_deploy_topology_round4_regression.py`: delete the marker
once fixed, never the test. Tests that pass are new guards for behavior
that is correct but was unpinned.

No Docker, no systemd, no real credentials: the shell tests execute the
real script under `/bin/sh` against synthetic fixtures, and the CLI tests
drive the real Typer app with the release runner injected.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.test_deploy_topology_round4_regression import _run_wrapper_with_backup_key

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "deploy" / "compose.yaml"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

runner = CliRunner()


# ---------------------------------------------------------------------------
# QA-43 — the NUL guard's own hardening reopened QA-34 at the last byte.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"goodkey\x00", b"\x00", b"good\x00"],
    ids=["nul-last-byte", "nul-only-byte", "short-value-nul-last"],
)
def test_wrapper_rejects_a_credential_whose_last_byte_is_a_nul(tmp_path: Path, raw: bytes) -> None:
    r"""A NUL *terminator* — the single most likely way a NUL reaches a
    credential file, since that is how any C-side or binary-safe secrets
    tool writes a string — lands at the end of the file, which is the one
    position the rewritten guard cannot see.

    `od -An -tx1` emits ` 67 6f 6f 64 6b 65 79 00\n`; `$(...)` strips that
    final newline, so `tr -s ' \n' ' '` has nothing left to turn into the
    trailing space `*' 00 '*` matches against. The pipeline form this
    replaced (`od | tr | grep -q ' 00 '`) kept the newline and caught it.

    Consequence is QA-34 verbatim: `BACKUP_ENCRYPTION_KEY` is exported as
    `goodkey` while `goodkey\x00` is what the owner recorded out-of-band,
    with no error and exit 0 — backups encrypted under a passphrase that
    is discovered to be wrong only at restore time.

    Fix by padding both ends before matching (`case " $hex " in *' 00 '*`)
    or restoring the pipeline form, not by trusting `$(...)` to preserve
    the byte stream's shape.
    """
    result = _run_wrapper_with_backup_key(tmp_path, raw)
    assert result.returncode != 0, (
        "wrapper exported a credential whose last byte is a NUL; the child received "
        f"0x{result.stdout.strip()} for an on-disk value of 0x{raw.hex()}"
    )
    assert "null byte" in result.stderr.lower() or "nul byte" in result.stderr.lower(), (
        f"rejected, but not as a NUL byte — the operator cannot act on this: {result.stderr!r}"
    )


def test_wrapper_still_rejects_a_nul_in_every_other_position(tmp_path: Path) -> None:
    """Guards the part of the NUL check that does hold, so the fix for the
    xfail above cannot regress the first/middle positions."""
    for raw in (b"\x00goodkey", b"good\x00key", b"goodkey\x00\x00", b"goodkey\x00\n"):
        result = _run_wrapper_with_backup_key(tmp_path, raw)
        assert result.returncode != 0, f"{raw!r} was exported: 0x{result.stdout.strip()}"


@pytest.mark.parametrize(
    "raw",
    [b"goodkey\x0c", b"goodkey\x0b", b"\x0cgoodkey"],
    ids=["trailing-formfeed", "trailing-vtab", "leading-formfeed"],
)
def test_wrapper_rejects_every_kind_of_edge_whitespace(tmp_path: Path, raw: bytes) -> None:
    """The script defines "whitespace" two different ways in the same file:
    `tr -d '[:space:]'` for the whitespace-only check (which therefore
    includes `\\v` and `\\f`), and a `case` on a literal space/tab for the
    edge-whitespace check. A value the first definition would call empty
    can still pass the second with real content attached to it. One
    definition, applied consistently — the corruption is identical
    whichever byte it is."""
    result = _run_wrapper_with_backup_key(tmp_path, raw)
    assert result.returncode != 0, (
        f"wrapper exported {raw!r} unchanged (child received 0x{result.stdout.strip()})"
    )


# ---------------------------------------------------------------------------
# QA-44 — one of five markup call sites was wrapped in `Text`.
# ---------------------------------------------------------------------------

_HOSTILE_SELFCHECK = json.dumps(
    {
        "release_id": "abc1234",
        "installed_release_id": "abc1234",
        "overall": "unhealthy",
        "migrations": {"status": "drift", "applied": "aaa", "head": "bbb"},
        # Every string in this payload comes off the release binary's own
        # stdout. CLAUDE.md treats every such string as hostile; an
        # unmatched Rich closing tag is the cheapest thing to put in one.
        "detail": "merchant [/red] note",
    }
)


def _hostile_run_release(_release_path, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
    if "selfcheck" in args:
        return subprocess.CompletedProcess(list(args), 0, _HOSTILE_SELFCHECK + "\n", "")
    return subprocess.CompletedProcess(list(args), 0, "", "")


class _FakeRow:
    """Stand-in for an `ops.releases` row — `_do_rollback` reads exactly
    these two attributes off whatever `get_previous` returns."""

    id = 7
    release_id = "abc1234"


@contextlib.contextmanager
def _fake_session_scope():  # noqa: ANN202
    yield object()


def _patch_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import finance_app.cli.finops as finops_module
    from finance_app.ops import release as release_ops

    monkeypatch.setattr(finops_module, "run_release", _hostile_run_release)
    monkeypatch.setattr(finops_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(finops_module, "observer_session_scope", _fake_session_scope)
    monkeypatch.setattr(release_ops, "get_previous", lambda _s: _FakeRow())
    # QA-47's `_do_rollback` also reads `get_current` (to capture
    # `expected_current_row_id` alongside the target) on the same fake
    # session `get_previous` above already stands in for — the payload
    # here is unhealthy either way, so `_do_rollback` raises
    # `RollbackTargetUnhealthyError` before this value is ever used; it
    # only needs to not crash the fake `object()` session on the way
    # there.
    monkeypatch.setattr(release_ops, "get_current", lambda _s: None)
    # `restart` reads `status.release_topology` (a bookkeeping/symlink
    # comparison), never `status.current_release` directly — patched here
    # so it doesn't try to run real SQL against the fake `object()` session
    # `_fake_session_scope` yields.
    monkeypatch.setattr(
        finops_module.status,
        "release_topology",
        lambda _s, *, release_root: {
            "bookkeeping_current": "abc1234",
            "symlink_current": "abc1234",
            "agrees": True,
        },
    )


@pytest.mark.parametrize("command", ["restart", "rollback"])
def test_operator_commands_survive_markup_shaped_probe_output(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """QA-12 established the rule — "`data` frequently carries strings
    sourced from the database ... per CLAUDE.md every such string is
    attacker-influenceable ... `Text` is never interpreted as markup" —
    and `_emit` follows it. `restart`/`rollback` do not.

    The probe payload is strictly *less* trustworthy than a database row:
    `probe_release` exists specifically to interrogate a release that has
    not yet been trusted. A release that reports itself unhealthy *and*
    embeds `[/red]` in any field turns the failure report into a
    `MarkupError` traceback with empty stdout — the operator is told
    nothing at the exact moment the gate fired correctly.

    Reproduced against the real Typer app; only the release runner and the
    DB sessions are stubbed. `--release-root` is set to something other
    than the production default so the ADR-019 production-opt-in guard
    doesn't refuse the command before ever reaching the mocked path.
    """
    _patch_cli(monkeypatch)
    import finance_app.cli.finops as finops_module

    result = runner.invoke(finops_module.app, [command, "--release-root", "/tmp/qa-release-root"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"`finops {command}` crashed on markup-shaped probe output: {result.exception!r}"
    )
    assert result.exit_code != 0
    assert result.output.strip(), (
        f"`finops {command}` exited non-zero with no output at all — nothing for an "
        "operator to act on"
    )


def test_deploy_prints_manual_intervention_when_the_rollback_target_is_also_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst possible moment to crash: the deploy failed its health
    check, the auto-rollback target failed *its* health check, and the one
    line that tells the operator production needs hands on it is built by
    f-string-interpolating the hostile payload into markup.

    `console.print("[red]post-deploy health check failed:[/red]",
    Text(str(health)))` — the line QA-42 fixed — is two lines above this
    one and proves the pattern was understood; this call site was missed.
    """
    import finance_app.cli.finops as finops_module
    from finance_app.ops import release as release_ops

    class _Deployed:
        id = 1
        release_id = "ccccccc"
        status = "pending"

    deployed = _Deployed()

    class _Session:
        def get(self, _model, _row_id):  # noqa: ANN001, ANN202
            return deployed

        def add(self, _obj):  # noqa: ANN001, ANN202
            pass

        def flush(self):  # noqa: ANN202
            pass

        def execute(self, *_a, **_kw):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("no database in this unit test")

    @contextlib.contextmanager
    def _scope():  # noqa: ANN202
        yield _Session()

    monkeypatch.setattr(finops_module, "run_release", _hostile_run_release)
    monkeypatch.setattr(finops_module, "session_scope", _scope)
    monkeypatch.setattr(finops_module, "observer_session_scope", _scope)
    # `deploy`'s two pre-flight gates (ADR-019, not present under the old
    # Docker model this test predates): confirm the release directory is
    # installed, and confirm a successful backup is on record. Both are
    # filesystem/DB checks this pure-mock test has no real backing for —
    # stubbed to their "proceed" answer so the test stays focused on the
    # markup/auto-rollback behavior below, not on building a real release
    # tree and a real backup row.
    monkeypatch.setattr(finops_module, "release_is_installed", lambda _root, _release_id: True)
    monkeypatch.setattr(finops_module, "latest_successful_backup", lambda: object())
    monkeypatch.setattr(
        release_ops, "start_deploy", lambda _s, *, release_id, artifact_ref: deployed
    )
    monkeypatch.setattr(
        release_ops, "mark_failed", lambda _s, r, *, reason: setattr(r, "status", "failed")
    )
    monkeypatch.setattr(release_ops, "mark_healthy", lambda _s, r: setattr(r, "status", "current"))
    monkeypatch.setattr(release_ops, "get_previous", lambda _s: _FakeRow())
    # QA-47's `_do_rollback` also reads `get_current` on the same fake
    # session `get_previous` above stands in for, to capture
    # `expected_current_row_id` — `_Session.execute` unconditionally
    # raises, and the real `get_previous` never gets that far, so this
    # only needs to not blow up on the way to the `RollbackTargetUnhealthyError`
    # this test actually exercises.
    monkeypatch.setattr(release_ops, "get_current", lambda _s: None)

    result = runner.invoke(
        finops_module.app, ["deploy", "ccccccc", "--release-root", "/tmp/qa-release-root"]
    )

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"`finops deploy` crashed while reporting a failed auto-rollback: {result.exception!r}"
    )
    assert "manual intervention" in result.output.lower(), (
        f"the operator was never told production needs manual intervention: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# QA-45 — the property QA-37's wrong-release fix depends on — under
# ADR-019, this is `test_wrong_release_detection_is_not_a_tautology` and
# `test_installed_release_root_walks_up_from_argv0_not_from_package_location`
# in `test_deploy_topology_baremetal.py`: an *executable* end-to-end test
# (real `git archive`, real `sys.argv[0]` forgery attempt) rather than a
# static grep over `deploy/compose.yaml`/`Dockerfile`/`ci.yml`, which no
# longer carry this identity at all — there is no image, no `ARG`/`ENV
# RELEASE_ID`, no `IMAGE_RELEASE_ID`. `test_selfcheck_reports_the_baked_
# identity_separately_from_the_injected_one` and
# `test_the_baked_image_identity_is_wired_end_to_end_across_three_files`
# (formerly here) are both superseded by that same file's tests — the
# `Settings.image_release_id` field they exercised no longer exists,
# deliberately (any `Settings` field is env-settable, i.e. forgeable by
# the very process `probe_release` is trying to verify; see
# `ops/identity.py`'s module docstring).
# ---------------------------------------------------------------------------


def test_deploy_reports_an_invalid_release_id_instead_of_crashing_on_it() -> None:
    """The same defect class as the two tests above, reachable with no
    Docker, no database, and no hostile image — just an argument:

        finops deploy '[/red]'

    `console.print(f"[red]not a valid release id ...:[/red] {release_id}")`
    parses the rejected argument as markup. The validator works; the way
    it reports its own rejection does not. Exit code 2 (a usage error, per
    this module's own convention for `--limit` out of range) is what the
    caller — including autonomous tooling reading `--json` output — is
    entitled to see.
    """
    import finance_app.cli.finops as finops_module

    result = runner.invoke(finops_module.app, ["deploy", "[/red]"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"`finops deploy` crashed while rejecting its own argument: {result.exception!r}"
    )
    assert result.exit_code == 2, result.output
    assert "not a valid release id" in result.output
