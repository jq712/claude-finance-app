"""Non-forgeable release identity for the bare-metal deploy gate (ADR-019,
carrying forward QA-37's invariant from the Docker model).

Under Docker, `probe_release`'s `wrong_image` check compared
`IMAGE_RELEASE_ID` — baked into the image at `docker build` time via `ARG
RELEASE_ID`/`ENV IMAGE_RELEASE_ID` — against the SHA the deploy requested.
That worked because the process *starting* the container could not forge
it: setting `RELEASE_ID` in the child's environment (which `probe_release`
does, so `finance selfcheck` can report it) is a completely different
value from what was burned into the image weeks earlier. QA-37 was
exactly the bug where an earlier version compared the SHA against itself.

`docs/deployment.md` claims the bare-metal model has "no equivalent need
... there is no image to mistake for another." That is wrong: `current`
can be repointed at the wrong release directory (by hand, by an
interrupted rollback, by a crash between the symlink swap and the
bookkeeping commit), and a release directory's *contents* can disagree
with its *name* (ADR-019's own "Consequences" section names this as the
new failure mode traded for dropping Docker — a partial or corrupted copy
is now this project's problem, not the container runtime's). So the same
non-forgeable-identity requirement still applies, and now has a second
call site the Docker model never needed: verifying `current`, not just a
freshly-deployed release.

The identity source: a plain file, `RELEASE_ID`, tracked at the repository
root with the literal content `$Format:%H$` and `.gitattributes` marking
it `export-subst`. `git archive <sha>` substitutes the real commit SHA
into that file *at archive time* — the mechanism `docs/runbooks/deploy.md`
already names as the release-copy step. A plain checkout (this dev tree,
`git clone`, `git worktree add`) never substitutes it, so it stays the
literal placeholder. That is what makes this non-forgeable: the value is
fixed inside the release tree before `finops deploy` ever runs, by a step
`finops` does not control — reading it back and comparing is not a
tautology, unlike comparing an env var against itself.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from finance_app.ops.host import RELEASES_DIRNAME

RELEASE_ID_FILENAME = "RELEASE_ID"

# The `git archive export-subst` placeholder, before substitution. If a
# release tree's RELEASE_ID file still contains this, it was never
# archived from a real commit — a plain checkout impersonating a release,
# or an aborted/incomplete copy — and must be treated as having no
# identity at all, never as a partial match.
_EXPORT_SUBST_PLACEHOLDER_PREFIX = "$Format:"

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def installed_release_root() -> Path | None:
    """The release directory the *currently running* `finance`/`finops`
    process was actually launched from — derived from `sys.argv[0]`
    (`<release_root>/releases/<sha>/.venv/bin/finance`), resolved through
    symlinks, three parents up (`.venv/bin/<name>` -> `.venv` ->
    `<release_path>`) — or `None` if that derivation cannot be trusted
    (see below).

    Deliberately not derived from `finance_app.__file__`: that resolves
    into `.venv/lib/python3.*/site-packages/finance_app/` once a release
    is built with `uv sync --locked --no-dev` (a non-editable install),
    which is nowhere near the release root. `sys.argv[0]` is exactly the
    path `ops/host.py:run_release` constructed to invoke this process, so
    walking up from it is correct regardless of how the package was
    installed — *when* it was invoked that way. Two shapes break the
    derivation silently rather than failing closed (qa-adversarial
    finding QA-56 on an earlier version of this function, which returned
    whatever three-parents-up produced with no check at all):

    - **A bare command name.** `sys.argv[0] == "finance"` (a systemd
      `ExecStart=` with no absolute path, or a `$PATH` lookup) resolves
      relative to the process's current working directory, not to any
      release tree — three parents up from `finance` (a lone path
      segment) lands wherever `cwd` happens to be, and whoever controls
      `cwd` then controls what identity gets reported.
    - **A relinked `.venv`.** If `.venv` itself is a symlink (a shared
      venv, a `cp -a` that preserved a symlink, a hand-repaired `uv sync`
      failure), resolving through it can land two levels above a
      directory that was never `releases/<sha>/` at all, and
      `read_release_identity` would then read whatever `RELEASE_ID` file
      happens to sit there — not the one `git archive` wrote for the
      release actually running.

    Resolving through symlinks is what makes this answer "which release
    is *actually* running" rather than "which path was named on the
    command line" (invoking via `<root>/current/.venv/bin/finance`
    reports the real directory `current` points at right now — exactly
    what a `current`-vs-bookkeeping disagreement check needs), but the
    same symlink-following that makes that case work is what lets a
    *wrong* symlink silently substitute a different directory. The check
    below closes that gap the only way that does not just move the
    trust problem somewhere else: confirm the derived grandparent
    directory is literally named `releases` (`RELEASES_DIRNAME`) — the
    one structural fact about a real release tree that a bare command
    name or an unrelated symlink target essentially never happens to
    reproduce by accident. A directory that fails this check reports
    `None`, and every caller already treats `None` as "no identity",
    failing the deploy/probe gate closed exactly as a missing or
    placeholder `RELEASE_ID` file would."""

    argv0 = sys.argv[0]
    if "/" not in argv0:
        # A lone command name with no directory component at all — e.g.
        # "finance" via a `$PATH` lookup with no absolute/relative path
        # given. The shell/OS resolved that via `$PATH`, which this
        # process has no record of; `Path(argv0).resolve()` would instead
        # (silently, incorrectly) resolve it relative to `cwd`, which is
        # not how it was actually found. Checking the *raw* string here,
        # not `Path(argv0).parts`, matters: `pathlib` normalizes away a
        # leading `./`, so `Path("./finance").parts == ("finance",)` —
        # the same length as a bare `"finance"` — even though `./finance`
        # *does* carry an explicit directory component (`.`, i.e. `cwd`)
        # that the shell resolved the identical way `Path.resolve()`
        # would. Refuse only the case with no separator at all.
        return None
    resolved = Path(argv0).resolve()
    if len(resolved.parents) < 3:
        return None
    candidate = resolved.parents[2]
    if candidate.parent.name != RELEASES_DIRNAME:
        return None
    return candidate


def read_release_identity(release_path: str | Path) -> str | None:
    """The `RELEASE_ID` file's content at `release_path`, or `None` if it
    is missing, unreadable, still the unsubstituted placeholder (a dev
    checkout, not an archived release), or not shaped like a Git SHA.

    Every failure mode returns `None` rather than raising — a missing or
    malformed identity file must fail the deploy/probe gate closed, not
    crash it; `ops/status.py`'s callers treat `None` the same way a
    disagreeing SHA is treated."""
    path = Path(release_path) / RELEASE_ID_FILENAME
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if content.startswith(_EXPORT_SUBST_PLACEHOLDER_PREFIX):
        return None
    if not _RELEASE_SHA_RE.match(content):
        return None
    return content


def installed_release_id() -> str | None:
    """`read_release_identity(installed_release_root())` — the identity of
    the release tree the current process is actually executing from. This
    is what `ops/selfcheck.py` reports and what `ops/status.py:probe_release`
    compares against the requested release id.

    `None` when `installed_release_root()` itself could not be trusted
    (a bare command name, a relinked `.venv`) — fails closed the same way
    a missing/placeholder `RELEASE_ID` file already does, rather than
    reading a file at a root that was never verified to be a release
    directory in the first place."""
    root = installed_release_root()
    if root is None:
        return None
    return read_release_identity(root)
