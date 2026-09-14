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

RELEASE_ID_FILENAME = "RELEASE_ID"

# The `git archive export-subst` placeholder, before substitution. If a
# release tree's RELEASE_ID file still contains this, it was never
# archived from a real commit — a plain checkout impersonating a release,
# or an aborted/incomplete copy — and must be treated as having no
# identity at all, never as a partial match.
_EXPORT_SUBST_PLACEHOLDER_PREFIX = "$Format:"

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def installed_release_root() -> Path:
    """The release directory the *currently running* `finance`/`finops`
    process was actually launched from — derived from `sys.argv[0]`
    (`<release_root>/releases/<sha>/.venv/bin/finance`), resolved through
    symlinks, three parents up (`.venv/bin/<name>` -> `.venv` ->
    `<release_path>`).

    Deliberately not derived from `finance_app.__file__`: that resolves
    into `.venv/lib/python3.*/site-packages/finance_app/` once a release
    is built with `uv sync --locked --no-dev` (a non-editable install),
    which is nowhere near the release root. `sys.argv[0]` is exactly the
    path `ops/host.py:run_release` constructed to invoke this process, so
    walking up from it is correct regardless of how the package was
    installed.

    Resolving through symlinks is what makes this answer "which release
    is *actually* running" rather than "which path was named on the
    command line" — invoking via `<root>/current/.venv/bin/finance`
    reports the real directory `current` points at right now, which is
    exactly what a `current`-vs-bookkeeping disagreement check needs."""
    return Path(sys.argv[0]).resolve().parents[2]


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
    compares against the requested release id."""
    return read_release_identity(installed_release_root())
