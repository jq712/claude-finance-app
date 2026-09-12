"""Integration tests for `ops.releases` bookkeeping (handoff §19, ADR-008)
— the state machine `finops deploy`/`rollback` drive. Pure database
operations (`src/finance_app/ops/release.py`), tested against a real
session rather than mocked, since the whole point is proving the
current/previous/failed/rolled_back transitions are correct."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from finance_app.ops import release as release_ops

pytestmark = pytest.mark.integration


def _release_id(release: release_ops.Release | None) -> str:
    assert release is not None
    return release.release_id


@pytest.fixture
def db_session(role_engine):
    engine = role_engine("finance_app")
    with Session(engine) as session:
        yield session
        session.rollback()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM ops.releases"))
            conn.commit()


def test_first_deploy_has_no_previous_release(db_session: Session) -> None:
    release = release_ops.start_deploy(db_session, release_id="a" * 40, image_ref="img:a" * 8)
    release_ops.mark_healthy(db_session, release)
    db_session.flush()

    assert _release_id(release_ops.get_current(db_session)) == "a" * 40
    assert release_ops.get_previous(db_session) is None


def test_second_healthy_deploy_demotes_current_to_previous(db_session: Session) -> None:
    first = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, first)
    db_session.flush()

    second = release_ops.start_deploy(db_session, release_id="2" * 40, image_ref="img:2")
    release_ops.mark_healthy(db_session, second)
    db_session.flush()

    assert _release_id(release_ops.get_current(db_session)) == "2" * 40
    assert _release_id(release_ops.get_previous(db_session)) == "1" * 40


def test_failed_deploy_does_not_disturb_current_or_previous(db_session: Session) -> None:
    first = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, first)
    db_session.flush()

    bad = release_ops.start_deploy(db_session, release_id="b" * 40, image_ref="img:b")
    release_ops.mark_failed(db_session, bad, reason="post-deploy health check failed")
    db_session.flush()

    assert _release_id(release_ops.get_current(db_session)) == "1" * 40
    assert release_ops.get_previous(db_session) is None
    assert bad.status == "failed"
    assert bad.health_check_status == "unhealthy"


def test_rollback_promotes_previous_and_marks_current_rolled_back(db_session: Session) -> None:
    first = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, first)
    db_session.flush()
    second = release_ops.start_deploy(db_session, release_id="2" * 40, image_ref="img:2")
    release_ops.mark_healthy(db_session, second)
    db_session.flush()

    promoted = release_ops.rollback(db_session, release_id=first.release_id)
    db_session.flush()

    assert promoted.release_id == "1" * 40
    assert _release_id(release_ops.get_current(db_session)) == "1" * 40
    assert second.status == "rolled_back"
    assert second.rolled_back_at is not None


def test_third_consecutive_healthy_deploy_demotes_oldest_to_plain_history(
    db_session: Session,
) -> None:
    """ADR-008 guarantees exactly one rollback step is trivial — current
    <-> previous — not an arbitrary-depth undo stack. A third consecutive
    healthy deploy should leave the first release as plain history: no
    longer `current` or `previous`, not silently still tracked as either."""
    first = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, first)
    db_session.flush()
    second = release_ops.start_deploy(db_session, release_id="2" * 40, image_ref="img:2")
    release_ops.mark_healthy(db_session, second)
    db_session.flush()
    third = release_ops.start_deploy(db_session, release_id="3" * 40, image_ref="img:3")
    release_ops.mark_healthy(db_session, third)
    db_session.flush()

    assert _release_id(release_ops.get_current(db_session)) == "3" * 40
    assert _release_id(release_ops.get_previous(db_session)) == "2" * 40
    assert first.status == "history"


def test_get_previous_is_none_after_a_single_deploy(db_session: Session) -> None:
    """The invariant `_do_rollback` (`cli.finops`) relies on to raise
    `NoPreviousReleaseError` before ever calling `rollback` (QA-41 moved
    that check to the caller, which resolves and health-verifies the
    target once rather than having `rollback` re-derive it) — a single
    deploy has nothing to fall back to."""
    only = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, only)
    db_session.flush()

    assert release_ops.get_previous(db_session) is None


def test_rollback_raises_when_the_target_release_id_is_not_tracked(db_session: Session) -> None:
    """`rollback` no longer resolves its own target (QA-41) — it promotes
    exactly the `release_id` it's given, which by construction is always
    one `_do_rollback` already found via `get_previous`. This is the
    defensive backstop for that contract being violated (a bug, or a
    release row deleted between resolution and promotion — rows here are
    never deleted in practice)."""
    with pytest.raises(release_ops.NoPreviousReleaseError):
        release_ops.rollback(db_session, release_id="9" * 40)


def test_deploy_auto_rollback_then_recovery_still_tracks_last_known_good(
    db_session: Session,
) -> None:
    """Simulates `finops deploy`'s auto-rollback path (ADR-008): a healthy
    release, then a failed deploy (auto-rolled-back — current/previous
    untouched), then another healthy deploy. Confirms `mark_failed` never
    corrupts the current/previous bookkeeping that a later successful
    deploy builds on."""
    good = release_ops.start_deploy(db_session, release_id="1" * 40, image_ref="img:1")
    release_ops.mark_healthy(db_session, good)
    db_session.flush()

    bad = release_ops.start_deploy(db_session, release_id="b" * 40, image_ref="img:b")
    release_ops.mark_failed(db_session, bad, reason="health check failed")
    db_session.flush()

    fixed = release_ops.start_deploy(db_session, release_id="2" * 40, image_ref="img:2")
    release_ops.mark_healthy(db_session, fixed)
    db_session.flush()

    assert _release_id(release_ops.get_current(db_session)) == "2" * 40
    assert _release_id(release_ops.get_previous(db_session)) == "1" * 40
