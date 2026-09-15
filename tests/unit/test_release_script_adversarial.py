"""Adversarial probes against `deploy/scripts/release.sh`.

Separate from `test_release_script.py` so the defect-demonstrating cases
stay legible as a set. Same execution model: real `/bin/sh`, real git,
synthetic repo, fake `uv` on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from finance_app.ops.host import release_is_installed

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
set -e
mkdir -p .venv/bin
printf '#!/bin/sh\\necho should never run\\n' > .venv/bin/finance
chmod +x .venv/bin/finance
exit 1
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


def _make_origin(d: Path) -> Path:
    d.mkdir(parents=True)
    _git("init", "-q", cwd=d)
    _write_minimal_release_tree(d)
    _commit_all(d, "init")
    _git("branch", "-m", "main", cwd=d)
    return d


def _make_mirror(d: Path, origin: Path) -> Path:
    d.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "--bare", str(d), cwd=d.parent)
    _git("remote", "add", "origin", str(origin), cwd=d)
    _git("fetch", "-q", "origin", cwd=d)
    return d


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _make_origin(tmp_path / "origin")


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    return _make_mirror(tmp_path / "mirror" / "repo.git", origin)


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


def _run(
    root: Path,
    repo_path: Path,
    sha: str,
    *extra_args: str,
    fake_uv: Path | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_uv is not None:
        env["PATH"] = f"{fake_uv}:{env['PATH']}"
    args = ["--release-root", str(root), "--repo", str(repo_path), *extra_args, sha]
    return subprocess.run(
        ["/bin/sh", str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Lead (a): a newline embedded in the <sha> argument
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "abcdef1\n../../etc",
        "abcdef1\n",
        "\nabcdef1",
        "abcdef1\nabcdef1",
        "abcdef1 ../../etc",
        "abcdef1\t..",
        "../../etc/passwd",
        "ABCDEF1",
        "abcdef1;rm -rf /",
        "$(id)",
        "abcdef1/../../..",
    ],
)
def test_a_hostile_sha_argument_is_refused_before_anything_is_created(
    release_root: Path, repo: Path, hostile: str, fake_uv_ok: Path
) -> None:
    """The `case "$sha" in *[!0-9a-f]*)` guard is claimed to catch values a
    line-oriented grep would not -- specifically a `<valid-sha>\\n<path>`
    argument. Passed through argv directly (subprocess list form), so the
    newline really is a single argv byte and not shell-quoted away."""
    result = _run(release_root, repo, hostile, fake_uv=fake_uv_ok)

    assert result.returncode == 2, (
        f"{hostile!r} was not refused with a usage error: "
        f"rc={result.returncode} out={result.stdout!r} err={result.stderr!r}"
    )
    assert "not a valid release id" in result.stderr
    assert list((release_root / "releases").iterdir()) == [], (
        "a refused sha must leave nothing behind in releases/"
    )


# ---------------------------------------------------------------------------
# Lead (b): paths containing spaces (and other shell-hostile characters)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weird", ["with space", "with'quote", "with*glob", "with[bracket]"])
def test_release_root_and_repo_paths_with_shell_hostile_characters(
    tmp_path: Path, weird: str
) -> None:
    """Every expansion in the script is double-quoted, including the
    `for entry in "$releases"/.staging.*` globs and the `case` patterns
    that embed `$release_path`. Prove it end to end: install, no-op
    re-run, repair, and --prune, all under a root and mirror whose paths
    contain a space (and a glob metacharacter, which would also break a
    naively-written `case` pattern)."""
    base = tmp_path / weird
    origin = _make_origin(base / "origin dir")
    mirror = _make_mirror(base / "mirror dir" / "repo.git", origin)
    root = base / "release root"
    (root / "releases").mkdir(parents=True)
    fake_uv = _fake_uv_bin(tmp_path, _FAKE_UV_OK)

    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()

    first = _run(root, mirror, full_sha, fake_uv=fake_uv)
    assert first.returncode == 0, first.stderr
    assert release_is_installed(root, full_sha) is True

    noop = _run(root, mirror, full_sha, fake_uv=fake_uv)
    assert noop.returncode == 0, noop.stderr
    assert "already installed" in noop.stdout, (
        "the no-op fast path (which `case`-matches $release_path) must still "
        f"fire under a path containing {weird!r}: {noop.stdout!r} {noop.stderr!r}"
    )

    # Repair path, plus prune, still under the hostile path.
    shutil.rmtree(root / "releases" / full_sha / ".venv")
    repaired = _run(root, mirror, full_sha, "--prune", "--keep", "1", fake_uv=fake_uv)
    assert repaired.returncode == 0, repaired.stderr
    assert release_is_installed(root, full_sha) is True


# ---------------------------------------------------------------------------
# Lead (c): the `*.superseded.*` dead-pid GC promotion branch
# ---------------------------------------------------------------------------

_DEAD_A = "999999990"
_DEAD_B = "999999991"


def _unrelated_commit(origin: Path, repo: Path, text: str) -> str:
    (origin / "n.txt").write_text(text)
    sha = _commit_all(origin, text)
    _git("fetch", "-q", "origin", cwd=repo)
    return sha


def test_two_dead_pid_superseded_backups_for_the_same_sha_leave_exactly_one_release(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """Two separate crashed repair attempts for the same sha. Whatever the
    tiebreak, the invariant is: the target is restored (not left absent),
    and no `*.superseded.*` debris survives the GC pass."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    for pid, marker in ((_DEAD_A, "copy-a"), (_DEAD_B, "copy-b")):
        d = releases / f"{full_sha}.superseded.{pid}"
        d.mkdir()
        (d / "marker.txt").write_text(marker)

    other = _unrelated_commit(origin, repo, "unrelated\n")
    result = _run(release_root, repo, other, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (releases / full_sha).is_dir(), "the release must have been restored from one backup"
    assert (releases / full_sha / "marker.txt").exists()
    leftovers = sorted(p.name for p in releases.glob("*.superseded.*"))
    assert leftovers == [], f"superseded debris survived the GC pass: {leftovers}"


def test_a_superseded_backup_is_not_discarded_when_the_target_is_a_dangling_symlink(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """The GC's "has something already taken its place?" test is
    `[ -e "$entry_target" ] || [ -L "$entry_target" ]`. A dangling symlink
    at `releases/<sha>` satisfies the `-L` half, so the backup -- possibly
    the only surviving copy of the release `current` points at -- is
    deleted, leaving `current` resolving to nothing at all. Nothing has
    actually "taken its place"."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    backup = releases / f"{full_sha}.superseded.{_DEAD_A}"
    backup.mkdir()
    (backup / "marker.txt").write_text("only surviving copy\n")
    (releases / full_sha).symlink_to(releases / "does-not-exist")

    other = _unrelated_commit(origin, repo, "unrelated\n")
    result = _run(release_root, repo, other, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (releases / full_sha / "marker.txt").exists() or backup.exists(), (
        "the only surviving copy of the release was destroyed because a dangling "
        "symlink at the target path was mistaken for a successful replacement"
    )


def test_a_superseded_backup_is_not_discarded_when_the_target_is_a_plain_file(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """Same branch, other degenerate target: a regular file at
    `releases/<sha>` satisfies `-e` and so counts as "already replaced",
    even though a release directory is not a file and nothing can run from
    it."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    backup = releases / f"{full_sha}.superseded.{_DEAD_A}"
    backup.mkdir()
    (backup / "marker.txt").write_text("only surviving copy\n")
    (releases / full_sha).write_text("")  # junk left by something else

    other = _unrelated_commit(origin, repo, "unrelated\n")
    result = _run(release_root, repo, other, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (releases / full_sha / "marker.txt").exists() or backup.exists(), (
        "the only surviving copy of the release was destroyed because a plain file "
        "at the target path was mistaken for a successful replacement"
    )


def test_a_restored_superseded_backup_is_then_repaired_in_the_same_run(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """End-to-end promotion: the GC restores the orphan, and the run that
    restored it -- for that very sha -- must then repair it to a real,
    installed release rather than trip over the tree it just put back."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    releases = release_root / "releases"
    backup = releases / f"{full_sha}.superseded.{_DEAD_A}"
    backup.mkdir()
    (backup / "marker.txt").write_text("stale\n")
    (release_root / "current").symlink_to(Path("releases") / full_sha, target_is_directory=True)

    result = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert release_is_installed(release_root, full_sha) is True
    assert not (releases / full_sha / "marker.txt").exists(), (
        "the restored stale tree must have been replaced by a fresh extraction"
    )
    assert sorted(p.name for p in releases.glob("*.superseded.*")) == []


# ---------------------------------------------------------------------------
# Repair-of-current: does a failed repair ever leave `current` worse off?
# ---------------------------------------------------------------------------


def test_prune_does_not_delete_a_real_release_because_of_word_splitting(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path
) -> None:
    """`for name in $(ls -1t "$releases")` is unquoted. The in-code comment
    justifies it as "safe to word-split here precisely because every name
    this loop acts on is validated as 40 hex characters below" -- but the
    validation happens AFTER splitting, so it validates the *fragments*, not
    the directory names. A single entry named `<sha> x` therefore yields a
    bare `<sha>` word that passes every check, consuming a `--keep` slot and
    then pruning the genuine release even though only `--keep` real releases
    exist."""
    releases = release_root / "releases"
    shas = []
    for i in range(3):
        shas.append(_unrelated_commit(origin, repo, f"commit {i}\n"))
        assert _run(release_root, repo, shas[-1], fake_uv=fake_uv_ok).returncode == 0
        time.sleep(0.02)

    oldest = shas[0]
    # An entry whose name splits into a valid-looking release id plus junk.
    decoy = releases / f"{oldest} x"
    decoy.mkdir()

    newest = _unrelated_commit(origin, repo, "commit 3\n")
    result = _run(release_root, repo, newest, "--prune", "--keep", "4", fake_uv=fake_uv_ok)

    assert result.returncode == 0, result.stderr
    assert (releases / oldest).is_dir(), (
        f"--keep 4 with 4 real releases pruned {oldest}: the unquoted `ls -1t` "
        f"word-split counted it twice. stdout={result.stdout!r}"
    )


def test_a_failed_repair_of_a_working_current_release_keeps_a_working_venv(
    release_root: Path, repo: Path, origin: Path, fake_uv_ok: Path, fake_uv_fails: Path
) -> None:
    """`already_installed` is 0 for a release whose venv works perfectly
    but whose RELEASE_ID disagrees with its directory name. That takes the
    same "move aside and rebuild" repair path. If the rebuild then fails
    AFTER the fresh extraction lands, cleanup() discards the backup on the
    grounds that the fresh tree "is already in the same safe 'exists but
    not installed' state the old copy was in" -- which is false here: the
    old copy WAS installed and running. The run therefore destroys a
    working venv under `current` and leaves nothing runnable."""
    full_sha = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    first = _run(release_root, repo, full_sha, fake_uv=fake_uv_ok)
    assert first.returncode == 0, first.stderr

    release_path = release_root / "releases" / full_sha
    (release_root / "current").symlink_to(Path("releases") / full_sha, target_is_directory=True)

    # The venv is intact and working; only the identity file disagrees.
    (release_path / "RELEASE_ID").write_text("0000000000000000000000000000000000000000\n")
    assert release_is_installed(release_root, full_sha) is True

    failed = _run(release_root, repo, full_sha, fake_uv=fake_uv_fails)

    assert failed.returncode == 1
    assert release_path.is_dir()
    assert release_is_installed(release_root, full_sha) is True, (
        "a failed repair left the release `current` points at with no venv at all, "
        "strictly worse than before the run: nothing to restart and nothing to roll "
        "back to"
    )
