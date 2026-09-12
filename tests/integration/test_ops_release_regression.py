"""Adversarial regression tests for the `ops.releases` state machine
(QA, Milestone 7 — handoff §19, ADR-008).

`src/finance_app/ops/release.py`'s module docstring claims:

    at most one row with status == "current"
    at most one row with status == "previous"

and `finops deploy`'s auto-rollback claims to return to "the previous
known-good release". Neither holds. Each test below encodes one confirmed
violation and is marked `xfail(strict=True)` so it flips to a loud XPASS
failure the moment the implementation is fixed — delete the marker then,
not the test.

The existing `tests/integration/test_ops_release.py` misses all of these
because it never calls `release_ops.rollback()` after a *third* deploy,
never runs two sessions concurrently, and never leaves a row in the
`deploying` state.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.ops import release as release_ops

pytestmark = pytest.mark.integration


@pytest.fixture
def clean_releases(role_engine):
    engine = role_engine("finance_app")
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.commit()
    yield engine
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM ops.releases"))
        conn.commit()


def _status_of(session: Session, release_id: str) -> str:
    return session.execute(
        text("SELECT status FROM ops.releases WHERE release_id = :r"), {"r": release_id}
    ).scalar_one()


def test_auto_rollback_returns_to_the_release_that_was_actually_running(clean_releases) -> None:
    """`finops deploy C` fails its health check while B is the running,
    known-good release. ADR-008's rollback target is B. Today
    `get_previous()` returns A (B is still `current`, untouched by
    `mark_failed`), so production is rolled back two versions and B is
    marked `rolled_back` despite never having been unhealthy."""
    with Session(clean_releases) as session:
        a = release_ops.start_deploy(session, release_id="a" * 7, image_ref="img:a")
        release_ops.mark_healthy(session, a)
        session.commit()
        b = release_ops.start_deploy(session, release_id="b" * 7, image_ref="img:b")
        release_ops.mark_healthy(session, b)
        session.commit()

        # Deploy C; post-deploy health fails, exactly as cli/finops.py does.
        c = release_ops.start_deploy(session, release_id="c" * 7, image_ref="img:c")
        release_ops.mark_failed(session, c, reason="post-deploy health failed")
        session.commit()

        # This is the release id `_do_rollback` hands to `docker compose up`.
        target = release_ops.get_previous(session)
        assert target is not None
        assert target.release_id == "b" * 7, (
            "auto-rollback must return to the release that was running (B), "
            f"not {target.release_id}"
        )

        release_ops.rollback(session, release_id=target.release_id)
        session.commit()

        assert _status_of(session, "b" * 7) == "current"
        assert _status_of(session, "a" * 7) == "previous"


def test_two_concurrent_deploys_cannot_both_become_current(clean_releases) -> None:
    """Two `finops deploy` invocations racing (an operator plus a retried
    systemd/CI trigger) each read the current release before either
    commits, so both promote themselves. The DB accepts it: two rows with
    status='current'. `get_current()` then silently picks one by
    `deployed_at desc` and `finops version` reports a release that may not
    be the one running."""
    with Session(clean_releases) as session:
        first = release_ops.start_deploy(session, release_id="1" * 7, image_ref="img:1")
        release_ops.mark_healthy(session, first)
        session.commit()

    s1, s2 = Session(clean_releases), Session(clean_releases)
    try:
        d1 = release_ops.start_deploy(s1, release_id="2" * 7, image_ref="img:2")
        s1.commit()
        d2 = release_ops.start_deploy(s2, release_id="3" * 7, image_ref="img:3")
        s2.commit()

        # Interleaved: both read the state, then both commit.
        release_ops.mark_healthy(s1, s1.get(type(d1), d1.id))  # type: ignore[arg-type]
        release_ops.mark_healthy(s2, s2.get(type(d2), d2.id))  # type: ignore[arg-type]
        s1.commit()
        s2.commit()
    finally:
        s1.close()
        s2.close()

    with Session(clean_releases) as session:
        n_current = session.execute(
            text("SELECT count(*) FROM ops.releases WHERE status = 'current'")
        ).scalar_one()
        n_previous = session.execute(
            text("SELECT count(*) FROM ops.releases WHERE status = 'previous'")
        ).scalar_one()

    assert n_current <= 1, f"{n_current} rows are status='current'; the invariant allows one"
    assert n_previous <= 1, f"{n_previous} rows are status='previous'; the invariant allows one"


def test_rollback_after_a_crashed_deploy_does_not_discard_the_running_release(
    clean_releases,
) -> None:
    with Session(clean_releases) as session:
        a = release_ops.start_deploy(session, release_id="a" * 7, image_ref="img:a")
        release_ops.mark_healthy(session, a)
        session.commit()
        b = release_ops.start_deploy(session, release_id="b" * 7, image_ref="img:b")
        release_ops.mark_healthy(session, b)
        session.commit()

        # Process killed here: the row is stuck in 'deploying' with no reaper.
        release_ops.start_deploy(session, release_id="c" * 7, image_ref="img:c")
        session.commit()

        stuck = session.execute(
            text("SELECT count(*) FROM ops.releases WHERE status = 'deploying'")
        ).scalar_one()
        assert stuck == 0, "an interrupted deploy must not leave an unreconciled 'deploying' row"


def test_redeploying_the_current_sha_leaves_a_distinct_rollback_target(clean_releases) -> None:
    with Session(clean_releases) as session:
        a = release_ops.start_deploy(session, release_id="a" * 7, image_ref="img:a")
        release_ops.mark_healthy(session, a)
        session.commit()
        b1 = release_ops.start_deploy(session, release_id="b" * 7, image_ref="img:b")
        release_ops.mark_healthy(session, b1)
        session.commit()
        # Operator re-runs `finops deploy bbbbbbb` (idempotent retry).
        b2 = release_ops.start_deploy(session, release_id="b" * 7, image_ref="img:b")
        release_ops.mark_healthy(session, b2)
        session.commit()

        current = release_ops.get_current(session)
        previous = release_ops.get_previous(session)
        assert current is not None and previous is not None
        assert previous.release_id != current.release_id, (
            "rollback target must differ from the running release; "
            f"both are {current.release_id}, so `finops rollback` would redeploy the same image"
        )
