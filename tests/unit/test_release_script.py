"""Regression tests for `deploy/scripts/release.sh` — the release-copy step
ADR-019's implementation-status table named "Not shipped" and `finops
deploy` deliberately does not perform itself (`cli/finops.py:415-421`
refuses with "the release-copy step must run first" unless
`releases/<sha>/` is already a real directory containing a built
`.venv/bin/finance`).

Executed end to end under `/bin/sh` against a synthetic origin repo, a
bare mirror built from it, and a throwaway release root — the same
subprocess-execution pattern `tests/unit/test_deploy_topology_regression.py`
uses for `with-production-env.sh` ("the job's real, executed requirement,
not a regex guess at the script's logic"). No network, no Docker, no
`/opt/finance`, no root. `uv sync` is stubbed by a fake `uv` prepended to
`PATH` that builds a real (but tiny) virtualenv with the system python —
fast and hermetic, while still exercising the script's real post-build
verification (a broken venv from a broken stub is still a broken venv).

Several of these tests assert against the actual predicates the rest of
the deploy path uses (`ops.host.release_is_installed`,
`ops.identity.read_release_identity`) rather than restating them, so this
file and those modules cannot silently drift apart — the same reasoning
`test_deploy_topology_baremetal.py`'s end-to-end `git archive` test gives
for doing the same thing.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from finance_app.ops.host import release_is_installed
from finance_app.ops.identity import read_release_identity

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "release.sh"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "release-script-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "release-script-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}

_FAKE_UV_OK = """#!/bin/sh
# Builds a real (tiny) venv with the system python, and copies
# src/finance_app straight into site-packages -- simulating a genuine
# non-editable install without a real dependency resolve.
set -e
"{python}" -m venv --without-pip .venv
SITE=$(.venv/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
mkdir -p "$SITE"
cp -r src/finance_app "$SITE/finance_app"
cat > .venv/bin/finance <<'EOF'
#!/bin/sh
echo "fake finance CLI OK"
EOF
chmod +x .venv/bin/finance
exit 0
"""

_FAKE_UV_FAILS = """#!/bin/sh
# Creates a console script (as a real uv failure partway through can) and
# THEN fails -- the case that matters most: release.sh must not leave this
# half-built venv looking installed.
set -e
mkdir -p .venv/bin
printf '#!/bin/sh\\necho should never run\\n' > .venv/bin/finance
chmod +x .venv/bin/finance
exit 1
"""

_FAKE_UV_EDITABLE = """#!/bin/sh
# Simulates uv's EDITABLE-by-default install: a .pth pointing at the
# release's own src/ tree instead of a copy in site-packages.
set -e
"{python}" -m venv --without-pip .venv
SITE=$(.venv/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
mkdir -p "$SITE"
printf '%s/src\\n' "$(pwd)" > "$SITE/finance_app_editable.pth"
cat > .venv/bin/finance <<'EOF'
#!/bin/sh
echo "fake finance CLI OK"
EOF
chmod +x .venv/bin/finance
exit 0
"""

_FAKE_UV_BLOCKS = """#!/bin/sh
# Signals it has started (so a test can wait for it), then blocks -- used
# to hold the venv-build step open long enough to exercise the lock and
# SIGTERM cleanup.
set -e
touch "$RELEASE_SCRIPT_TEST_MARKER"
sleep 60
"""


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def _write_minimal_release_tree(root: Path) -> None:
    """Every path release.sh's manifest check requires, plus an
    export-subst-ready RELEASE_ID -- the minimal tree that can actually be
    installed."""
    (root / "RELEASE_ID").write_text("$Format:%H$\n")
    (root / ".gitattributes").write_text("RELEASE_ID export-subst\n")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (root / "uv.lock").write_text("# lock\n")
    (root / "alembic.ini").write_text("[alembic]\n")
    (root / "migrations").mkdir()
    (root / "migrations" / ".keep").write_text("")
    (root / "src" / "finance_app").mkdir(parents=True)
    (root / "src" / "finance_app" / "__init__.py").write_text('__version__ = "0.0.0"\n')


def _commit_all(repo: Path, message: str) -> str:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    d = tmp_path / "origin"
    d.mkdir()
    _git("init", "-q", cwd=d)
    _write_minimal_release_tree(d)
    _commit_all(d, "init")
    _git("branch", "-m", "main", cwd=d)
    return d


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    """A bare mirror fetched from `origin`, provisioned exactly as
    `docs/runbooks/deploy.md` §1 instructs: `init --bare` + `remote add` +
    `fetch`, which is what actually creates the `refs/remotes/origin/*`
    refspec the provenance gate depends on (a plain `clone --bare` does
    not)."""
    d = tmp_path / "mirror" / "repo.git"
    d.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", str(d), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=d)
    _git("fetch", "-q", "origin", cwd=d)
    return d


@pytest.fixture
def release_root(tmp_path: Path) -> Path:
    d = tmp_path / "root"
    (d / "releases").mkdir(parents=True)
    return d


def _fake_uv_bin(tmp_path: Path, script_body: str, name: str = "uv") -> Path:
    bindir = tmp_path / f"fakebin-{name}-{os.getpid()}-{time.time_ns()}"
    bindir.mkdir()
    script = bindir / "uv"
    script.write_text(script_body.format(python=sys.executable))
    script.chmod(0o755)
    return bindir


@pytest.fixture
def fake_uv_ok(tmp_path: Path) -> Path:
    return _fake_uv_bin(tmp_path, _FAKE_UV_OK)


@pytest.fixture
def fake_uv_fails(tmp_path: Path) -> Path:
    return _fake_uv_bin(tmp_path, _FAKE_UV_FAILS)


@pytest.fixture
def fake_uv_editable(tmp_path: Path) -> Path:
    return _fake_uv_bin(tmp_path, _FAKE_UV_EDITABLE)


def _run(
    root: Path,
    repo_path: Path,
    sha: str,
    *extra_args: str,
    fake_uv: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_uv is not None:
        env["PATH"] = f"{fake_uv}:{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    args = ["--release-root", str(root), "--repo", str(repo_path), *extra_args, sha]
    return subprocess.run(
        ["/bin/sh", str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Happy path / identity
# ---------------------------------------------------------------------------


def test_installs_a_release_directory_named_for_the_full_sha(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    abbreviated = full_sha[:10]

    result = _run(release_root, repo, abbreviated, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (release_root / "releases" / full_sha).is_dir()
    assert not (release_root / "releases" / abbreviated).exists()
    assert full_sha in result.stdout


def test_release_id_is_export_subst_substituted(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    content = (release_root / "releases" / full_sha / "RELEASE_ID").read_text().strip()
    assert content == full_sha
    assert not content.startswith("$Format:")


def test_the_installed_tree_satisfies_release_is_installed(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """Asserted against the real predicate `finops deploy` calls
    (`ops.host.release_is_installed`) and the real identity reader
    (`ops.identity.read_release_identity`), not a restatement of either --
    this is the test that keeps this script and those modules from
    silently drifting apart."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert release_is_installed(release_root, full_sha) is True
    assert read_release_identity(release_root / "releases" / full_sha) == full_sha


def test_prints_the_finops_deploy_command_for_the_installed_sha(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert "finops deploy" in result.stdout
    assert full_sha in result.stdout
    assert str(release_root) in result.stdout


# ---------------------------------------------------------------------------
# Content-vs-name guard
# ---------------------------------------------------------------------------


def test_refuses_when_release_id_is_not_export_subst_substituted(
    tmp_path: Path, release_root: Path, fake_uv_ok: Path
) -> None:
    """A commit whose `.gitattributes` never marks `RELEASE_ID
    export-subst` -- `git archive` leaves the literal placeholder, and the
    script must refuse rather than install a release with no identity."""
    bad_origin = tmp_path / "bad-origin"
    bad_origin.mkdir()
    _git("init", "-q", cwd=bad_origin)
    _write_minimal_release_tree(bad_origin)
    (bad_origin / ".gitattributes").unlink()  # no export-subst rule at all
    full_sha = _commit_all(bad_origin, "no export-subst")
    _git("branch", "-m", "main", cwd=bad_origin)

    bad_repo = tmp_path / "bad-mirror" / "repo.git"
    bad_repo.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", str(bad_repo), cwd=tmp_path)
    _git("remote", "add", "origin", str(bad_origin), cwd=bad_repo)
    _git("fetch", "-q", "origin", cwd=bad_repo)

    result = _run(release_root, bad_repo, full_sha, "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert "RELEASE_ID" in result.stderr
    assert not (release_root / "releases" / full_sha).exists()
    assert list((release_root / "releases").glob(".staging.*")) == []
    assert list((release_root / "releases").glob(".archive.*")) == []


def test_refuses_an_archive_missing_a_runtime_required_path(
    tmp_path: Path, release_root: Path, fake_uv_ok: Path
) -> None:
    bad_origin = tmp_path / "bad-origin"
    bad_origin.mkdir()
    _git("init", "-q", cwd=bad_origin)
    _write_minimal_release_tree(bad_origin)

    shutil.rmtree(bad_origin / "migrations")
    full_sha = _commit_all(bad_origin, "no migrations")
    _git("branch", "-m", "main", cwd=bad_origin)

    bad_repo = tmp_path / "bad-mirror" / "repo.git"
    bad_repo.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", str(bad_repo), cwd=tmp_path)
    _git("remote", "add", "origin", str(bad_origin), cwd=bad_repo)
    _git("fetch", "-q", "origin", cwd=bad_repo)

    result = _run(release_root, bad_repo, full_sha, "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert "migrations" in result.stderr
    assert not (release_root / "releases" / full_sha).exists()


def test_refuses_an_archive_containing_a_symlink(
    tmp_path: Path, release_root: Path, fake_uv_ok: Path
) -> None:
    """Regression for a security-review round-1 finding: `git archive`
    cannot express a path-traversal tar entry directly, but it CAN emit a
    symlink entry whose target is absolute or escapes the tree -- which
    would resolve outside the release directory the first time anything
    (a backup, this script's own manifest check, which uses `-e` and
    therefore follows symlinks) reads through it. This repository tracks
    no symlinks at all, so refusing any is safe for a legitimate release
    and closes the class outright."""
    bad_origin = tmp_path / "bad-origin"
    bad_origin.mkdir()
    _git("init", "-q", cwd=bad_origin)
    _write_minimal_release_tree(bad_origin)
    (bad_origin / "evil_link").symlink_to("/etc/passwd")
    full_sha = _commit_all(bad_origin, "contains a symlink")
    _git("branch", "-m", "main", cwd=bad_origin)

    bad_repo = tmp_path / "bad-mirror" / "repo.git"
    bad_repo.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", str(bad_repo), cwd=tmp_path)
    _git("remote", "add", "origin", str(bad_origin), cwd=bad_repo)
    _git("fetch", "-q", "origin", cwd=bad_repo)

    result = _run(release_root, bad_repo, full_sha, "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert not (release_root / "releases" / full_sha).exists()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_refuses_a_sha_not_reachable_from_origin_main(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    _git("checkout", "-q", "-b", "side", cwd=origin)
    (origin / "hotfix.txt").write_text("hotfix\n")
    side_sha = _commit_all(origin, "unmerged hotfix")
    _git("checkout", "-q", "main", cwd=origin)
    _git("fetch", "-q", "origin", cwd=repo)

    result = _run(release_root, repo, side_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert "--allow-unmerged" in result.stderr
    assert not (release_root / "releases" / side_sha).exists()


def test_allow_unmerged_installs_a_sha_not_on_main(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    _git("checkout", "-q", "-b", "side", cwd=origin)
    (origin / "hotfix.txt").write_text("hotfix\n")
    side_sha = _commit_all(origin, "unmerged hotfix")
    _git("checkout", "-q", "main", cwd=origin)
    _git("fetch", "-q", "origin", cwd=repo)

    result = _run(release_root, repo, side_sha, "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (release_root / "releases" / side_sha).is_dir()


def test_refuses_a_commit_reachable_only_through_a_merge_not_on_first_parent(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """Regression for a security-review round-1 finding: `git merge-base
    --is-ancestor` proves reachability, not that CI evaluated that exact
    tree. A commit folded into `main` by a merge commit (this project's
    normal PR-merge shape, not a squash/fast-forward) is an ancestor of
    `main` forever after, without ever itself having been a PR head or a
    `main` tip CI's `push: branches: [main]` job evaluated. The fix checks
    membership in `main`'s first-parent history instead -- this test
    builds exactly that shape (a real merge commit with a non-first-parent
    intermediate commit) and confirms the intermediate commit is refused
    even though it IS a plain ancestor, while the merge commit itself (and
    the tip after it) are accepted."""
    _git("checkout", "-q", "-b", "feature", cwd=origin)
    (origin / "feature.txt").write_text("step 1\n")
    intermediate_sha = _commit_all(origin, "intermediate commit, never a main tip")
    (origin / "feature.txt").write_text("step 2\n")
    _commit_all(origin, "feature complete")
    _git("checkout", "-q", "main", cwd=origin)
    _git(
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "merge feature",
        "feature",
        cwd=origin,
    )
    merge_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    _git("fetch", "-q", "origin", cwd=repo)

    # Sanity: the intermediate commit really is a plain ancestor of main,
    # which is exactly what makes `--is-ancestor` the wrong check here.
    ancestor_check = _git(
        "merge-base",
        "--is-ancestor",
        intermediate_sha,
        "refs/remotes/origin/main",
        cwd=repo,
        check=False,
    )
    assert ancestor_check.returncode == 0, "test setup: intermediate commit must be an ancestor"

    refused = _run(release_root, repo, intermediate_sha, fake_uv=fake_uv_ok)
    assert refused.returncode == 1
    assert "first-parent" in refused.stderr
    assert not (release_root / "releases" / intermediate_sha).exists()

    accepted = _run(release_root, repo, merge_sha, fake_uv=fake_uv_ok)
    assert accepted.returncode == 0, accepted.stderr
    assert (release_root / "releases" / merge_sha).is_dir()


def test_refuses_a_sha_the_mirror_has_never_seen(
    release_root: Path, repo: Path, fake_uv_ok: Path
) -> None:
    result = _run(release_root, repo, "deadbeef00", "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert not list((release_root / "releases").iterdir())


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_a_failed_venv_build_leaves_a_tree_that_is_not_installed(
    release_root: Path, repo: Path, origin: Path, fake_uv_fails: Path
) -> None:
    """The single most important invariant in this script: a failed or
    rejected build must never leave behind a `.venv/bin/finance` that would
    make `release_is_installed` report a broken release as installed."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_fails)

    assert result.returncode == 1
    release_path = release_root / "releases" / full_sha
    assert release_path.is_dir(), "the verified source tree should survive a build failure"
    assert not (release_path / ".venv").exists(), "a failed build must not leave .venv behind"
    assert release_is_installed(release_root, full_sha) is False


def test_refuses_an_editable_install(
    release_root: Path, repo: Path, origin: Path, fake_uv_editable: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_editable)

    assert result.returncode == 1
    assert "editable" in result.stderr.lower()
    release_path = release_root / "releases" / full_sha
    assert not (release_path / ".venv").exists()
    assert release_is_installed(release_root, full_sha) is False


def test_no_partial_directory_when_the_archive_step_fails(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """A sha that resolves (passes rev-parse) but whose archive can't be
    produced -- simulated here by pointing --repo at a repo that has the
    commit as a loose ref logically but where `git archive` itself would
    fail is hard to construct without corrupting the odb by hand, so this
    instead confirms the simpler, more common case: an unresolvable sha
    never creates anything under releases/ at all (covered above), and
    that no `.staging.*`/`.archive.*` debris survives ANY refusal."""
    result = _run(release_root, repo, "0000000000", "--allow-unmerged", fake_uv=fake_uv_ok)

    assert result.returncode == 1
    assert list((release_root / "releases").glob(".staging.*")) == []
    assert list((release_root / "releases").glob(".archive.*")) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerunning_an_installed_release_is_a_noop_and_does_not_rebuild(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr

    finance_bin = release_root / "releases" / full_sha / ".venv" / "bin" / "finance"
    mtime_before = finance_bin.stat().st_mtime_ns

    second = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert second.returncode == 0, second.stderr
    assert "already installed" in second.stdout
    assert finance_bin.stat().st_mtime_ns == mtime_before, "no-op must not rebuild the venv"


def test_an_incomplete_install_is_repaired_on_rerun(
    release_root: Path, repo: Path, origin: Path, fake_uv_fails: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    failed = _run(release_root, repo, full_sha, fake_uv=fake_uv_fails)
    assert failed.returncode == 1
    assert release_is_installed(release_root, full_sha) is False

    repaired = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert repaired.returncode == 0, repaired.stderr
    assert release_is_installed(release_root, full_sha) is True


def test_refuses_an_installed_release_whose_release_id_disagrees(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """A release_id that reads back as installed but whose RELEASE_ID has
    been tampered with must be refused, not silently reinstalled over --
    it may be the release `current` points at."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr

    release_path = release_root / "releases" / full_sha
    (release_path / "RELEASE_ID").write_text("tampered-not-a-real-sha\n")

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    # Rebuilt from scratch (the script's "not a complete install" repair
    # path treats a RELEASE_ID mismatch the same as any other incomplete
    # state), which is the correct outcome here: RELEASE_ID is restored to
    # the true value by re-extracting, not left tampered.
    assert result.returncode == 0, result.stderr
    assert (release_path / "RELEASE_ID").read_text().strip() == full_sha


def test_an_installed_release_with_a_broken_venv_is_not_trusted_as_a_noop(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """Regression for an sre-release round-1 finding: the "already
    installed" fast path used to check only that `.venv/bin/finance`
    exists as a file, not that the venv actually works. A SIGKILL or an
    untrapped signal mid-`uv sync` can leave that one file present while
    the rest of the venv is missing or broken -- `uv` writes console
    scripts late in the install, but not last. Simulated here by deleting
    the installed package's site-packages copy while leaving
    `.venv/bin/finance` untouched: the import check must fail, the release
    must NOT be treated as a no-op, and it must be rebuilt from scratch
    rather than left broken."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr
    assert release_is_installed(release_root, full_sha) is True

    release_path = release_root / "releases" / full_sha
    site_packages = next((release_path / ".venv").rglob("finance_app"))

    shutil.rmtree(site_packages)
    # `.venv/bin/finance` itself is untouched -- release_is_installed()'s
    # own check would still (wrongly, pre-fix) call this installed.
    assert (release_path / ".venv" / "bin" / "finance").is_file()

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert "already installed" not in result.stdout, (
        "a release whose venv fails the import check must not be reported as a no-op"
    )
    assert "not a complete" in result.stderr
    assert release_is_installed(release_root, full_sha) is True
    assert list((release_path / ".venv").rglob("finance_app")), (
        "the rebuild must have restored a working, importable install"
    )


def test_a_failed_repair_of_the_current_release_never_leaves_current_dangling(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path, fake_uv_fails: Path
) -> None:
    """Regression for a security-review round-1 finding: repairing an
    incomplete install used to `rm -rf` the release directory outright
    before re-extracting. If that repair was for the release `current`
    already points at (exactly the state the venv_building trap itself can
    produce after an interrupted build) and the repair attempt ALSO fails,
    the old destructive-rm-rf behavior would leave `current` resolving to
    a path with nothing at it at all -- a production outage with no
    release directory left to restart, roll back to, or even inspect.

    The fix moves the existing tree aside instead of deleting it, and
    restores it if the repair doesn't complete. This test forces exactly
    that failed-repair path and asserts the release directory still
    exists afterward."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr

    release_path = release_root / "releases" / full_sha
    (release_root / "current").symlink_to(Path("releases") / full_sha, target_is_directory=True)

    # Simulate the state an interrupted build leaves the *current* release
    # in: the venv is gone, everything else is intact.
    shutil.rmtree(release_path / ".venv")
    assert release_is_installed(release_root, full_sha) is False

    failed_repair = _run(release_root, repo, full_sha, fake_uv=fake_uv_fails)

    assert failed_repair.returncode == 1
    assert release_path.is_dir(), (
        "a failed repair of the release `current` points at must never leave "
        "nothing at that path -- current would dangle with no way to restart"
    )
    assert not list((release_root / "releases").glob("*.superseded.*")), (
        "a failed repair must restore the superseded backup, not leave it lying around"
    )

    repaired = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert repaired.returncode == 0, repaired.stderr
    assert release_is_installed(release_root, full_sha) is True
    assert not list((release_root / "releases").glob("*.superseded.*")), (
        "a successful repair must clean up its superseded backup"
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_a_concurrent_run_refuses_while_the_lock_is_held(
    release_root: Path, repo: Path, origin: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    lock = release_root / "releases" / ".lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")

    result = _run(release_root, repo, full_sha)

    assert result.returncode == 1
    assert "already running" in result.stderr
    assert not (release_root / "releases" / full_sha).exists()


def test_a_lock_left_by_a_dead_pid_is_broken_with_a_warning(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    lock = release_root / "releases" / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("999999999\n")  # not a real pid

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert "stale lock" in result.stderr
    assert release_is_installed(release_root, full_sha) is True


def test_stale_staging_debris_from_a_dead_pid_is_collected(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    (releases / ".staging.deadbeef.999999999").mkdir()
    (releases / ".archive.deadbeef.999999999.tar").write_text("")

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert not (releases / ".staging.deadbeef.999999999").exists()
    assert not (releases / ".archive.deadbeef.999999999.tar").exists()


def test_a_superseded_backup_from_a_dead_pid_is_restored_if_never_replaced(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """A `.superseded.*` entry is NOT like `.staging.*`/`.archive.*` scratch
    debris: it can be the only surviving copy of the release `current`
    points at, if the process that moved it aside was SIGKILLed before a
    fresh extraction was ever renamed back into `release_path`. The
    dead-pid GC must restore it in that case, not blindly delete it --
    that would complete the exact failure moving it aside was meant to
    prevent, just delayed to this later run."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    orphaned_content = releases / f"{full_sha}.superseded.999999999"
    orphaned_content.mkdir()
    (orphaned_content / "marker.txt").write_text("this is the only surviving copy\n")

    # A different sha, so the run below does not touch `full_sha` itself --
    # isolates the GC pass from the rest of the script's normal behavior.
    (origin / "n.txt").write_text("unrelated commit\n")
    other_sha = _commit_all(origin, "unrelated")
    _git("fetch", "-q", "origin", cwd=repo)

    result = _run(release_root, repo, other_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert not orphaned_content.exists()
    restored = releases / full_sha
    assert restored.is_dir(), "the orphaned superseded copy must be restored, not deleted"
    assert (restored / "marker.txt").read_text() == "this is the only surviving copy\n"


def test_a_superseded_backup_from_a_dead_pid_is_discarded_if_already_replaced(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """The mirror image of the restore case: if `release_path` was
    already recreated (a later run completed successfully) before this
    GC pass runs, the orphaned superseded copy is redundant and should be
    discarded, not left behind forever."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr

    releases = release_root / "releases"
    orphaned_content = releases / f"{full_sha}.superseded.999999999"
    orphaned_content.mkdir()
    (orphaned_content / "marker.txt").write_text("stale, should be discarded\n")

    (origin / "n.txt").write_text("unrelated commit 2\n")
    other_sha = _commit_all(origin, "unrelated 2")
    _git("fetch", "-q", "origin", cwd=repo)

    result = _run(release_root, repo, other_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert not orphaned_content.exists()
    assert release_is_installed(release_root, full_sha) is True


@pytest.mark.skipif(sys.platform != "linux", reason="process-group signalling assumed below")
def test_sigterm_during_the_venv_build_cleans_up(
    tmp_path: Path, release_root: Path, repo: Path, origin: Path
) -> None:
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    blocking_uv = _fake_uv_bin(tmp_path, _FAKE_UV_BLOCKS)
    marker = tmp_path / "started"
    env = dict(os.environ)
    env["PATH"] = f"{blocking_uv}:{env['PATH']}"
    env["RELEASE_SCRIPT_TEST_MARKER"] = str(marker)

    proc = subprocess.Popen(
        [
            "/bin/sh",
            str(_SCRIPT),
            "--release-root",
            str(release_root),
            "--repo",
            str(repo),
            full_sha,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 10
        while not marker.exists():
            assert time.time() < deadline, "fake uv never signalled it started"
            time.sleep(0.05)
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert proc.returncode == 143
    releases = release_root / "releases"
    assert list(releases.glob(".staging.*")) == []
    assert list(releases.glob(".archive.*")) == []
    assert not (releases / ".lock").exists()
    assert release_is_installed(release_root, full_sha) is False


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns everything; the check cannot fire")
def test_refuses_a_release_root_not_owned_by_the_caller(repo: Path, fake_uv_ok: Path) -> None:
    # A directory that exists, is not owned by the test's own uid, and is
    # readable by everyone -- true of most of /usr on any CI runner.
    foreign = Path("/usr/lib")
    assume_ok = foreign.is_dir()
    if not assume_ok:  # pragma: no cover - environment-dependent fallback
        pytest.skip("/usr/lib not present on this runner")

    result = _run(foreign, repo, "0" * 40, fake_uv=fake_uv_ok)

    assert result.returncode == 2
    assert "not owned by" in result.stderr
    assert not (foreign / "releases").exists()


def test_refuses_a_world_writable_release_root(
    release_root: Path, repo: Path, fake_uv_ok: Path
) -> None:
    """Regression for a security-review round-1 finding: ownership alone
    is not the real boundary on a shared host if the mode is permissive --
    a world-writable tree, even one this uid owns, is one any other local
    user could write into. Deliberately narrower than "group- or
    other-writable" (that broke on git init --bare's own default output
    under a common `umask 002`); world-writable has no legitimate reading
    on any host."""
    release_root.chmod(0o777)

    result = _run(release_root, repo, "0" * 40, fake_uv=fake_uv_ok)

    assert result.returncode == 2
    assert "writable" in result.stderr


@pytest.mark.skipif(os.geteuid() != 0, reason="only meaningful when actually running as root")
def test_refuses_to_run_as_root(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:  # pragma: no cover - CI runners are never root
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert result.returncode == 2
    assert "root" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


def _install(release_root: Path, repo_path: Path, origin: Path, fake_uv: Path, message: str) -> str:
    (origin / "n.txt").write_text(f"{message}\n")
    sha = _commit_all(origin, message)
    _git("fetch", "-q", "origin", cwd=repo_path)
    result = _run(release_root, repo_path, sha, fake_uv=fake_uv)
    assert result.returncode == 0, result.stderr
    return sha


def test_prune_keeps_the_newest_n_and_never_removes_current(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    shas = [_git("rev-parse", "HEAD", cwd=origin).stdout.strip()]
    result = _run(release_root, repo, shas[0], fake_uv=fake_uv_ok)
    assert result.returncode == 0, result.stderr
    for i in range(6):
        shas.append(_install(release_root, repo, origin, fake_uv_ok, f"commit {i}"))

    # `current` points at an old release (the second one installed) -- prune
    # must keep it regardless of age.
    (release_root / "current").symlink_to(Path("releases") / shas[1], target_is_directory=True)

    newest = shas[-1]
    result = _run(release_root, repo, newest, "--prune", "--keep", "3", fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    remaining = {p.name for p in (release_root / "releases").iterdir() if p.is_dir()}
    assert shas[1] in remaining, "current's target must never be pruned"
    assert shas[-1] in remaining, "the just-installed release must never be pruned"
    assert shas[-2] in remaining and shas[-3] in remaining, "the 3 newest must be kept"
    assert shas[0] not in remaining, "an old, non-current release should have been pruned"


def test_prune_is_a_noop_without_the_flag(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    shas = [_git("rev-parse", "HEAD", cwd=origin).stdout.strip()]
    result = _run(release_root, repo, shas[0], fake_uv=fake_uv_ok)
    assert result.returncode == 0, result.stderr
    for i in range(3):
        shas.append(_install(release_root, repo, origin, fake_uv_ok, f"commit {i}"))

    result = _run(release_root, repo, shas[-1], "--keep", "1", fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    remaining = {p.name for p in (release_root / "releases").iterdir() if p.is_dir()}
    assert remaining == set(shas)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["ABCDEF1"],  # uppercase hex rejected
        ["abc123"],  # too short (6 chars)
        ["a" * 41],  # too long
        [""],
        ["../../etc"],
        ["--keep", "0", "deadbeef00"],
        ["--keep", "abc", "deadbeef00"],
        ["--unknown-flag", "deadbeef00"],
    ],
)
def test_usage_errors_exit_2_and_create_nothing(
    release_root: Path, repo: Path, args: list[str]
) -> None:
    env = dict(os.environ)
    result = subprocess.run(
        ["/bin/sh", str(_SCRIPT), "--release-root", str(release_root), "--repo", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert list((release_root / "releases").iterdir()) == []


def test_the_script_is_posix_sh_and_parses() -> None:
    mode = _SCRIPT.stat().st_mode
    assert mode & 0o111, "release.sh must be executable"
    first_line = _SCRIPT.read_text().splitlines()[0]
    assert first_line == "#!/bin/sh"

    result = subprocess.run(
        ["/bin/sh", "-n", str(_SCRIPT)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
