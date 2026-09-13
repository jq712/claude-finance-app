"""Adversarial regression tests for ADR-016's round-2 rollback fix
(QA round 4, Milestone 7 — handoff §19, ADR-008, ADR-016 D2/D3).

Round 2's fix (b) made `_do_rollback` run `status.probe_release` against
the rollback target and raise `RollbackTargetUnhealthyError` instead of
promoting a target that fails its own selfcheck. Round 4 attacked that fix
and found:

1. The only test covering it
   (`test_deploy_gate_regression.py::test_rollback_refuses_to_promote_a_
   target_that_fails_its_own_selfcheck`) cannot fail. `_do_rollback` calls
   `status.probe_release(...)` *without* passing `run_compose_fn`, so the
   `monkeypatch.setattr(finops_module, "run_compose", ...)` that test
   installs is never consulted. The non-zero exit it asserts on comes from
   `ComposeError("docker CLI not found")`, not from the guard — and on any
   host that does have Docker (every GitHub-hosted runner), the test shells
   out to a real `docker compose -f deploy/compose.yaml --profile app run
   --rm -T app finance selfcheck --json` against the production topology.
2. The probe and the promotion resolve their target independently, in two
   different sessions, so what gets promoted need not be what was verified.
3. A deploy interrupted between `start_deploy` and `mark_healthy`/
   `mark_failed` leaves a `pending` row that makes the *next* `finops
   rollback` skip past the release that is actually current — QA-2's
   failure class, reachable again through a hole the round-2 fix did not
   close.

Defect tests are `xfail(strict=True)`, matching the convention in
`test_deploy_gate_regression.py`: delete the marker once fixed, never the
test.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from finance_app.ops import status as status_module

pytestmark = pytest.mark.integration

runner = CliRunner()


def _release_rows(engine) -> dict[str, str]:
    with engine.begin() as conn:
        return dict(
            conn.execute(text("SELECT release_id, status FROM ops.releases")).all()  # type: ignore[arg-type]
        )


@pytest.fixture
def two_healthy_releases(role_engine):
    """`aaaaaaa` (previous) -> `bbbbbbb` (current), with `bbbbbbb` carrying
    the `replaces_release_id` a real `start_deploy` would have recorded —
    the ordinary state a third deploy or a rollback starts from."""
    engine = role_engine("finance_app")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.execute(
            text(
                "INSERT INTO ops.releases "
                "(release_id, image_ref, status, health_check_status, replaces_release_id) "
                "VALUES ('aaaaaaa', 'img:aaaaaaa', 'previous', 'healthy', NULL), "
                "       ('bbbbbbb', 'img:bbbbbbb', 'current', 'healthy', 'aaaaaaa')"
            )
        )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.releases"))


def _selfcheck_stub(overall: str):  # noqa: ANN202
    """A `run_compose` stand-in that answers a `finance selfcheck --json`
    run with the pinned `RELEASE_ID` and the given verdict, and records
    every call so a test can prove whether it was consulted at all."""
    calls: list[tuple] = []

    def fake(compose_file, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        calls.append(args)
        if "selfcheck" in args:
            reported = (env or {}).get("RELEASE_ID")
            # `image_release_id` matches the requested id here (QA-37):
            # this fixture stands in for a release image that genuinely
            # *is* the one requested, so it must not read as `wrong_image`
            # — these tests are about QA-40's runner-injection and QA-41's
            # verify-then-promote race, not the image-identity check.
            payload = json.dumps(
                {"release_id": reported, "image_release_id": reported, "overall": overall}
            )
            return subprocess.CompletedProcess(
                list(args), 0 if overall == "healthy" else 1, payload + "\n", ""
            )
        return subprocess.CompletedProcess(list(args), 0, "", "")

    return fake, calls


@pytest.fixture
def no_real_docker(monkeypatch, tmp_path):
    """Guarantees the tests below cannot reach a real Docker daemon.

    Not defensive padding: `_do_rollback`/`restart` bypass every injected
    runner (QA-40 below), so on a host that *does* have Docker — every
    GitHub-hosted runner — they otherwise shell out to `docker compose -f
    deploy/compose.yaml --profile app run --rm -T app finance selfcheck
    --json`, which starts the production `postgres` service and tries to
    pull a release image, on the test host. An empty `PATH` turns that into
    a deterministic `FileNotFoundError` instead, so these tests assert the
    same thing, at the same speed, with or without a daemon present. The
    existing
    `test_deploy_gate_regression.py::test_rollback_refuses_to_promote_a_target_that_fails_its_own_selfcheck`
    has no such guard — see QA-40.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))


# ---------------------------------------------------------------------------
# 1. The rollback/restart probe is not injectable, so its test is vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [["rollback"], ["restart"]])
def test_rollback_and_restart_use_the_injected_compose_runner(
    two_healthy_releases, no_real_docker, monkeypatch, command: list[str]
) -> None:
    """`deploy` threads its compose runner through explicitly
    (`deploy_health_check(..., run_compose_fn=run_compose)`), which is why
    `test_finops_cli_regression.py`'s deploy tests genuinely exercise the
    gate. `_do_rollback` and `restart` do not:

        health = status.probe_release(release_id=target_release_id,
                                      compose_file=compose_file)

    `probe_release`'s `run_compose_fn` default is bound to
    `ops.compose.run_compose` at function-definition time, so neither
    patching `cli.finops.run_compose` nor patching `ops.compose.run_compose`
    after import changes what these two commands execute. Consequences:

    * the round-2 fix for `_do_rollback` has no test that can fail — swap
      its `unhealthy` fake for a `healthy` one and it still passes, because
      the non-zero exit comes from "docker CLI not found";
    * on a runner that *does* have Docker, that test really runs
      `docker compose -f deploy/compose.yaml --profile app run --rm -T app
      finance selfcheck --json`, which starts the production `postgres`
      service (`depends_on: service_healthy`) and attempts to pull
      `ghcr.io/jq712/claude-finance-app:aaaaaaa` on the test host.

    Fix by passing `run_compose_fn=run_compose` from both call sites, the
    way `deploy` already does.
    """
    fake, calls = _selfcheck_stub("healthy")
    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_compose", fake)

    runner.invoke(finops_module.app, command)

    assert calls, (
        f"`finops {command[0]}` never called the injected compose runner — it shelled out "
        "to the real `docker` binary, so no test of this path can distinguish a working "
        "probe from a broken one"
    )


def test_rollback_refuses_an_unhealthy_target_because_it_is_unhealthy(
    two_healthy_releases, no_real_docker, monkeypatch
) -> None:
    """The assertion the round-2 test *meant* to make: the refusal must be
    attributable to the target's own selfcheck verdict, not to any failure
    of the probe to run. Pins the message the operator sees, too — "rollback
    target is not healthy: aaaaaaa: {... 'status': 'unhealthy' ...}", never
    "unreachable"."""
    fake, _calls = _selfcheck_stub("unhealthy")
    import finance_app.cli.finops as finops_module

    monkeypatch.setattr(finops_module, "run_compose", fake)

    result = runner.invoke(finops_module.app, ["rollback"])

    assert result.exit_code != 0
    assert _release_rows(two_healthy_releases)["bbbbbbb"] == "current"
    assert "'status': 'unhealthy'" in result.output.replace("\n", ""), (
        f"refusal was not attributed to the target's selfcheck verdict: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# 2. Verify-then-promote is not atomic: the promoted release may not be the
#    probed one.
# ---------------------------------------------------------------------------


def test_rollback_promotes_the_same_release_it_verified(two_healthy_releases, monkeypatch) -> None:
    """`_do_rollback` is verify-then-act across two transactions:

        with session_scope() as session:
            previous = release_ops.get_previous(session)   # resolves target A
            target_release_id = previous.release_id
        health = status.probe_release(release_id=target_release_id, ...)
        with session_scope() as session:
            release_ops.rollback(session)                  # resolves target *again*
        return target_release_id                           # reports A either way

    `get_previous` is state-dependent — it returns the tracked `previous`
    row normally, but the tracked `current` row when the newest release is
    `failed` — so any release-table change during the probe window (an
    image pull and container start: seconds to minutes) silently changes
    what the second call resolves. The probe then certifies one release
    while a different one is promoted, and the operator is told the first
    one was rolled back.

    Reproduced deterministically here by having a concurrent failed deploy
    land while the probe is running. The second resolution flips to
    `bbbbbbb`, nothing is promoted at all, and the CLI still prints
    "rolled back to aaaaaaa" and exits 0.

    Resolve the target once and pass it through — `release_ops.rollback`
    should take the release id that was verified, not re-derive it.
    """
    engine = two_healthy_releases
    probed: list[str] = []

    def probe_and_race(*, release_id: str, **_kwargs):  # noqa: ANN202
        probed.append(release_id)
        # A concurrent `finops deploy` fails and records itself while this
        # probe is in flight. `mark_failed` deliberately touches neither
        # current nor previous, but it does change what `get_previous`
        # resolves.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops.releases "
                    "(release_id, image_ref, status, health_check_status, replaces_release_id) "
                    "VALUES ('ccccccc', 'img:ccccccc', 'failed', 'unhealthy', 'bbbbbbb')"
                )
            )
        return {"status": "healthy", "reported_release_id": release_id, "detail": {}}

    monkeypatch.setattr(status_module, "probe_release", probe_and_race)
    import finance_app.cli.finops as finops_module

    result = runner.invoke(finops_module.app, ["rollback"])

    statuses = _release_rows(engine)
    assert probed == ["aaaaaaa"]
    reported = result.output.strip()
    assert statuses["aaaaaaa"] == "current", (
        f"probed {probed[0]!r} but {reported!r} while ops.releases says {statuses} — the "
        "release that was health-verified is not the release that was promoted"
    )


# ---------------------------------------------------------------------------
# 3. An interrupted deploy poisons the rollback target.
# ---------------------------------------------------------------------------


def test_rollback_after_an_interrupted_deploy_does_not_skip_the_current_release(
    two_healthy_releases, monkeypatch
) -> None:
    """`finops deploy ccccccc` killed (SIGKILL, VPS reboot, OOM, or any
    exception the deploy path does not catch — see
    `test_deploy_leaves_no_release_pending_when_the_health_probe_raises`
    below) after `start_deploy` and before `mark_healthy`/`mark_failed`
    leaves `ccccccc` `pending`.

    `get_previous` special-cases only `failed`:

        if latest is not None and latest.status == "failed":
            ... return the current release ...
        return _tracked_previous(session)

    so with a `pending` latest it falls through to the tracked `previous`
    row and `finops rollback` promotes `aaaaaaa`, demoting `bbbbbbb` — the
    release that is actually deployed — to `rolled_back`. That is QA-2
    ("a failed deploy rolled back to the wrong release") reached through a
    different door.

    `_reap_stale_pending_deploys` does not save this: it runs only inside
    `start_deploy`, so it never executes on the rollback path at all, and
    its 15-minute grace window would not cover the incident-response case
    anyway (kill a stuck deploy, roll back immediately).

    `get_previous` must treat an unresolved `pending` attempt the same way
    it treats a `failed` one — whatever is `current` is still what is
    running.
    """
    engine = two_healthy_releases
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ops.releases "
                "(release_id, image_ref, status, replaces_release_id) "
                "VALUES ('ccccccc', 'img:ccccccc', 'pending', 'bbbbbbb')"
            )
        )

    monkeypatch.setattr(
        status_module,
        "probe_release",
        lambda *, release_id, **_kw: {
            "status": "healthy",
            "reported_release_id": release_id,
            "detail": {},
        },
    )
    import finance_app.cli.finops as finops_module

    runner.invoke(finops_module.app, ["rollback"])

    statuses = _release_rows(engine)
    assert statuses["bbbbbbb"] != "rolled_back", (
        "rollback demoted the release that is actually current and promoted the one "
        f"before it, because an interrupted deploy left a `pending` row: {statuses}"
    )


def test_deploy_leaves_no_release_pending_when_the_health_probe_raises(
    two_healthy_releases, monkeypatch
) -> None:
    """`deploy` guards the pull/migrate step with `except ComposeError` and
    marks the release `failed`, but the health-check phase that follows has
    no guard at all. `run_compose` only converts
    `CalledProcessError`/`TimeoutExpired`/`FileNotFoundError` into
    `ComposeError` (see
    `tests/unit/test_deploy_topology_round4_regression.py::
    test_run_compose_wraps_every_subprocess_failure_as_a_compose_error`),
    so a `PermissionError` on the Docker socket propagates straight out of
    `finops deploy`:

    * the operator gets a raw traceback from the one command that is
      supposed to be the narrow, auditable production interface;
    * the release stays `pending`, so no auto-rollback runs even though the
      deploy demonstrably did not succeed;
    * and that `pending` row then mis-targets the next `finops rollback`
      (test above).

    Whatever the failure, the release must end up `failed` — the deploy
    either succeeded or it did not.
    """
    import finance_app.cli.finops as finops_module

    def permission_denied(compose_file, *args, env=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
        if "selfcheck" in args:
            raise PermissionError(13, "Permission denied", "/var/run/docker.sock")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(finops_module, "run_compose", permission_denied)

    runner.invoke(finops_module.app, ["deploy", "c" * 7])

    statuses = _release_rows(two_healthy_releases)
    assert statuses.get("ccccccc") != "pending", (
        f"deploy crashed and left the release unresolved: {statuses}"
    )
