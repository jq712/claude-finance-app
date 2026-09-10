"""Unit tests for `finops deploy`'s release-id validation — no database,
no Docker required. `finops deploy` must refuse anything that isn't
Git-SHA-shaped *before* touching `ops.releases` or `docker compose`, since
ADR-008 requires every release to be identified by an immutable Git SHA,
never an arbitrary string (and never `latest`, even though the image
carries that tag too — see `src/finance_app/cli/finops.py`'s module
docstring)."""

from typer.testing import CliRunner

from finance_app.cli.finops import app

runner = CliRunner()


def test_deploy_rejects_a_non_sha_release_id() -> None:
    result = runner.invoke(app, ["deploy", "not-a-sha"])

    assert result.exit_code == 2
    assert "not a valid release id" in result.output


def test_deploy_rejects_the_latest_tag_explicitly() -> None:
    """`latest` is published as a convenience tag (ci.yml) but must never
    be accepted as a deploy target — ADR-008's "never latest as the sole
    production identifier" is enforced here, not just documented."""
    result = runner.invoke(app, ["deploy", "latest"])

    assert result.exit_code == 2


def test_deploy_accepts_a_short_sha_shape() -> None:
    """A 7-40 character hex string is accepted by the validator itself
    (whether the deploy actually succeeds depends on Docker/DB, which
    this test doesn't exercise) — confirms the regex isn't over-strict
    about SHA length."""
    from finance_app.cli.finops import _RELEASE_ID_RE

    assert _RELEASE_ID_RE.match("a1b2c3d")
    assert _RELEASE_ID_RE.match("a" * 40)
    assert not _RELEASE_ID_RE.match("a" * 41)
    assert not _RELEASE_ID_RE.match("a1b2c3")  # 6 chars, too short
    assert not _RELEASE_ID_RE.match("not-hex-at-all")
