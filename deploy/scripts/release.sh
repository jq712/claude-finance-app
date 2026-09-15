#!/bin/sh
# Copy a CI-green git ref into a new bare-metal release directory and build
# its virtualenv (ADR-019's release step 2-3, the row its implementation-status
# table called "Release-copy script (deploy/scripts/release.sh or equivalent)").
#
# This is the step `finops deploy` deliberately does NOT do. `finops deploy`
# refuses with "the release-copy step must run first" unless
# `<release-root>/releases/<sha>/` is already a real directory containing a
# built `.venv/bin/finance` (`ops/host.py:release_is_installed`). This script
# is what puts it there. The two halves meet at that predicate and nowhere
# else — this script never touches `current`, never touches the database,
# never restarts anything.
#
#   Usage: release.sh [options] <sha>
#
#     --release-root PATH  release tree root (default /opt/finance)
#     --repo PATH          bare mirror to archive from
#                          (default <release-root>/repo.git)
#     --allow-unmerged     skip the origin/main ancestry gate (hotfix escape)
#     --no-fetch           don't `git fetch` first (offline redeploy)
#     --prune              after a successful install, delete old releases
#     --keep N             with --prune, how many to keep (default 5)
#
# Runs as the production Unix user (`finance-prod`), on the VPS, invoked by
# the owner — never by CI and never by the Claude Code engineering session,
# which cannot read /opt/finance at all (ADR-007/ADR-010/ADR-019,
# docs/runbooks/deploy.md). It refuses to run as root, because root-owned
# files under /opt/finance would be unmanageable by finance-prod afterwards.
#
# Why `git archive` and not rsync/cp/clone. The repository's root `RELEASE_ID`
# file contains the literal `$Format:%H$` and `.gitattributes` marks it
# `export-subst`, so `git archive` substitutes the real commit SHA into it *at
# archive time*. `ops/identity.py` treats an unsubstituted placeholder as no
# identity at all and fails the deploy health gate closed — so a copy made any
# other way produces a release that can never pass `finops deploy`. That
# substitution is the non-forgeable release identity (QA-37's invariant,
# carried over from the Docker model's baked IMAGE_RELEASE_ID).
#
# Why the sha is expanded to its full 40 characters. `ops/status.py:probe_release`
# compares the requested release id against the RELEASE_ID read back out of the
# tree with an exact string comparison, and export-subst always writes the full
# %H. A release installed under an abbreviated name would therefore always
# probe as `wrong_release`. Expanding here means `finops deploy <full-sha>` is
# the only shape that ever exists on disk.
#
# Ordering note, load-bearing: the archive is extracted into a staging
# directory and *renamed* into place, then the virtualenv is built in the final
# location. The reverse (build then rename) is broken — a venv's console
# scripts carry an absolute-path shebang pointing at the directory they were
# built in, so renaming afterwards silently breaks every entry point. The
# intermediate state this ordering leaves behind (a complete source tree with
# no venv yet) is the safe one: `release_is_installed` reports false, `finops
# deploy` refuses, and re-running this script repairs it.
set -eu

PROGRAM=$(basename "$0")

DEFAULT_RELEASE_ROOT=/opt/finance

release_root=$DEFAULT_RELEASE_ROOT
repo=
sha=
allow_unmerged=0
do_fetch=1
do_prune=0
keep=5

# Set once the lock/staging/venv paths actually exist, so the EXIT trap only
# ever removes things this invocation created — and, for venv_building,
# cleared only once every post-build check has passed, so the trap removes an
# unfinished or failed-verification venv rather than leaving behind a
# `.venv/bin/finance` that would make `release_is_installed` report a broken
# release as installed (the exact hazard the atomic-rename ordering exists to
# avoid for the extraction half; this is its counterpart for the build half).
lock=
lock_held=0
staging=
tmp_tar=
venv_building=
# Set only when repairing the exact release `current` points at (see
# "Already installed?" below); if this run dies before the repair
# completes, the EXIT trap restores this back to its normal path so
# `current` never resolves to nothing, even though what's restored may
# still be the same broken install that prompted the repair.
superseded=
# Whether the tree `superseded` holds was a genuinely working install at
# the moment it was moved aside (checked before the move, never assumed).
# `cleanup()` must never discard a verified-working backup just because
# an unverified fresh attempt happens to occupy `release_path` when this
# run ends (adversarial review, round 2).
superseded_was_verified=0
# Declared here (rather than first assigned mid-script) purely so `set -u`
# never trips if `cleanup` fires before the normal assignment further down
# has run — `cleanup` reads it to decide whether to restore `superseded`.
release_path=

die() {
    printf '%s: %s\n' "$PROGRAM" "$1" >&2
    exit "${2:-1}"
}

usage() {
    sed -n '/^#   Usage:/,/^#     --keep/p' "$0" | sed 's/^# \{0,3\}//'
    exit 2
}

cleanup() {
    # No `exit` in here, deliberately: called both from the EXIT trap
    # (where the shell's own exit status must pass through unaltered —
    # POSIX preserves it automatically as long as nothing here overrides
    # it) and, explicitly, from each signal-specific trap below, which
    # disables every trap first and sets its own 128+signo exit code
    # afterward. A signal delivered between two ordinary commands (not
    # while a child is running) would otherwise report whatever `$?` the
    # last command happened to leave — possibly 0 — rather than the fact
    # that this run was interrupted (security review, round 1, LOW).
    if [ -n "$staging" ]; then
        if [ -d "$staging" ]; then
            rm -rf "$staging"
        fi
    fi
    if [ -n "$tmp_tar" ]; then
        if [ -f "$tmp_tar" ]; then
            rm -f "$tmp_tar"
        fi
    fi
    if [ -n "$venv_building" ]; then
        if [ -d "$venv_building" ]; then
            rm -rf "$venv_building"
        fi
    fi
    if [ -n "$superseded" ] && [ -d "$superseded" ] && [ -n "$release_path" ]; then
        if [ "$superseded_was_verified" -eq 0 ] && [ -d "$release_path" ] &&
            [ ! -L "$release_path" ]; then
            # The backup was NOT a verified-working install when it was
            # moved aside, and a proper directory (not a symlink, not a
            # plain file — adversarial review, round 2: `-e`/`-L` alone
            # treated a dangling symlink or a stray file as "already
            # replaced" and discarded the only real backup under it)
            # already occupies `release_path` — the failure was later,
            # e.g. `uv sync` itself. That fresh tree is in the same safe
            # "exists but not verified" state the old copy was in, so the
            # old copy is now redundant, not a fallback.
            rm -rf "$superseded"
        else
            # Either the backup WAS verified-working (never discard that
            # for an unverified replacement, regardless of what — if
            # anything — sits at release_path right now: adversarial
            # review, round 2 found the round-1 fix's discard branch
            # destroying a working release this way), or release_path is
            # missing, a symlink, or a plain file — none of which can
            # possibly be a working release. Clear whatever degenerate
            # thing is there (a plain `mv` onto an existing non-directory
            # path fails outright) and restore.
            rm -rf "$release_path" 2>/dev/null
            mv "$superseded" "$release_path"
        fi
    fi
    if [ "$lock_held" -eq 1 ]; then
        rm -rf "$lock" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --release-root)
            [ $# -ge 2 ] || die "--release-root requires a path" 2
            release_root=$2
            shift 2
            ;;
        --repo)
            [ $# -ge 2 ] || die "--repo requires a path" 2
            repo=$2
            shift 2
            ;;
        --keep)
            [ $# -ge 2 ] || die "--keep requires a number" 2
            keep=$2
            shift 2
            ;;
        --allow-unmerged)
            allow_unmerged=1
            shift
            ;;
        --no-fetch)
            do_fetch=0
            shift
            ;;
        --prune)
            do_prune=1
            shift
            ;;
        -h | --help)
            usage
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option: $1" 2
            ;;
        *)
            break
            ;;
    esac
done

[ $# -eq 1 ] || usage
sha=$1

# ---------------------------------------------------------------------------
# Validation, before anything touches the filesystem
# ---------------------------------------------------------------------------

if [ "$(id -u)" -eq 0 ]; then
    die "refusing to run as root — run as the production user, e.g.
  sudo -u finance-prod $0 <sha>
Root-owned files under $DEFAULT_RELEASE_ROOT cannot be managed by that user afterwards." 2
fi

# A second, narrower form of the same check, and the only one of the two that
# CI can actually exercise (CI never runs as root, so the check above never
# fires there): every path this script writes to must already be owned by
# the user running it. `-O` is POSIX-adjacent and supported by dash/bash/
# ksh/busybox alike. Checked once release_root is known to exist below.
_require_owned_by_self() {
    [ -e "$1" ] || return 0
    if [ ! -O "$1" ]; then
        die "$1 is not owned by $(id -un) — refusing to write into a release tree owned by
another user. This usually means --release-root (or --repo) points somewhere other
than the intended production user's own tree." 2
    fi
    # Ownership alone is not the real boundary on a shared host if the mode
    # is permissive enough to let a different Unix user write into it —
    # but this check is deliberately narrower than "group- or
    # other-writable": `git init --bare` and a plain `mkdir` both produce
    # group-writable directories under an ordinary `umask 002` (verified),
    # which is a common default and not itself wrong — the documented
    # invariant this project actually relies on (docs/runbooks/deploy.md
    # §1) is that the engineering session's Unix user is not a *member* of
    # `finance-prod`'s group, checked operationally there, not by this
    # script guessing at an acceptable mode. World-writable has no such
    # legitimate reading on any host and is refused outright.
    if [ -n "$(find "$1" -maxdepth 0 -perm /002 2>/dev/null)" ]; then
        die "$1 is other/world-writable — refusing to treat it as a private release tree.
Fix its mode (chmod o-w) before retrying." 2
    fi
}

# The two checks that prove a release directory's venv is genuinely usable,
# not just present: the console script itself actually runs (a venv built
# somewhere else, or truncated mid-build, has a broken absolute-path
# shebang), and the release's own interpreter imports finance_app from
# inside .venv/ rather than the release's own src/ tree (a real, executed
# check of --no-editable's effect). Used identically in three places: the
# already-installed no-op check, deciding whether a release about to be
# superseded was itself a working install (adversarial review, round 2 —
# see the header note by that name below), and verifying a fresh build.
_venv_is_usable() {
    "$1/.venv/bin/finance" --help >/dev/null 2>&1 || return 1
    _venv_imported_from=$("$1/.venv/bin/python" -c \
        'import finance_app, pathlib; print(pathlib.Path(finance_app.__file__).resolve())' \
        2>/dev/null) || return 1
    case "$_venv_imported_from" in
        "$1"/.venv/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Deliberately a `case` glob rather than a grep: a value containing a newline
# would be split into lines by grep, so a `<valid-sha>\n../../etc` argument
# could pass a line-oriented check and then be used as a path component. A
# bracket-negated glob matches the whole string, newline included.
case "$sha" in
    *[!0-9a-f]*) die "not a valid release id (expected 7-40 lowercase hex characters)" 2 ;;
esac
if [ "${#sha}" -lt 7 ] || [ "${#sha}" -gt 40 ]; then
    die "not a valid release id (expected 7-40 lowercase hex characters)" 2
fi

case "$keep" in
    '' | *[!0-9]*) die "--keep must be a positive integer" 2 ;;
esac
[ "$keep" -ge 1 ] || die "--keep must be at least 1 (the newest release is never pruned)" 2

# Before anything under $release_root is created — including `releases/`
# and the lock directory a few lines down — so nothing inherits the
# caller's (potentially permissive) umask instead of this script's own.
umask 027

[ -n "$repo" ] || repo=$release_root/repo.git
releases=$release_root/releases

# Never created by this script, even via `mkdir -p`'s side effect of
# creating missing parents: provisioning `$release_root` itself is an
# explicit owner-performed step (docs/runbooks/deploy.md §1), and a typo'd
# --release-root should refuse loudly rather than quietly populate a new
# tree wherever it happened to point.
[ -d "$release_root" ] || die "$release_root does not exist — provision it first
(see docs/runbooks/deploy.md §1). This script never creates the release root itself." 2
_require_owned_by_self "$release_root"

[ -d "$repo" ] || die "$repo is not a directory — the bare mirror has not been provisioned
(see docs/runbooks/deploy.md §1)."
git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 ||
    die "$repo is not a git repository."
_require_owned_by_self "$repo"

command -v uv >/dev/null 2>&1 || die "uv is not on PATH — the release virtualenv cannot be built."

mkdir -p "$releases"
chmod 0750 "$releases"
_require_owned_by_self "$releases"
[ -w "$releases" ] || die "$releases is not writable by $(id -un)."

# ---------------------------------------------------------------------------
# Lock. `mkdir` is the portable atomic test-and-set; `flock` is not guaranteed
# present and this is /bin/sh. Two concurrent runs must not interleave an
# extraction with a virtualenv build.
# ---------------------------------------------------------------------------

lock=$releases/.lock
if ! mkdir "$lock" 2>/dev/null; then
    # Held already — but by a live process, or debris from one that was
    # SIGKILLed/lost power before its own EXIT trap (installed a few lines
    # below, right after the first successful mkdir) ever ran? Only a
    # provably dead pid is treated as stale; an unreadable pid file is
    # refused rather than guessed at, and a live pid is always refused, even
    # if it looks to have been running a long time — this script has no way
    # to know what a long-running fetch/uv-sync on a slow link looks like
    # from outside.
    holder_pid=$(cat "$lock/pid" 2>/dev/null || true)
    case "$holder_pid" in
        '' | *[!0-9]*)
            die "$lock exists and its pid file is missing or unreadable. If no $PROGRAM is
actually running, remove it by hand: rm -rf $lock"
            ;;
    esac
    if kill -0 "$holder_pid" 2>/dev/null; then
        die "another $PROGRAM (pid $holder_pid) is already running against $release_root
(lock: $lock)."
    fi
    printf '%s: breaking a stale lock left by dead pid %s\n' "$PROGRAM" "$holder_pid" >&2
    rm -rf "$lock"
    mkdir "$lock" 2>/dev/null || die "lost the race for $lock — another $PROGRAM just started."
fi
lock_held=1
printf '%s\n' "$$" >"$lock/pid"
trap cleanup EXIT
# Each signal gets its own trap rather than sharing the EXIT one: disabling
# every trap before re-exiting means `cleanup` runs exactly once (POSIX
# would otherwise fire the EXIT trap a second time when this trap's own
# `exit` executes), and setting the exit code explicitly (128+signo, the
# usual convention) is what makes a between-commands signal report as
# interrupted rather than as whatever `$?` happened to be at that moment.
trap 'trap - EXIT HUP INT QUIT TERM; cleanup; exit 129' HUP
trap 'trap - EXIT HUP INT QUIT TERM; cleanup; exit 130' INT
trap 'trap - EXIT HUP INT QUIT TERM; cleanup; exit 131' QUIT
trap 'trap - EXIT HUP INT QUIT TERM; cleanup; exit 143' TERM

# Debris from a run that died the same way (SIGKILL, power loss) rather than
# exiting through its own trap: a `.staging.*`/`.archive.*` entry survives
# only its own process, so once the pid encoded in its name is provably dead,
# it is safe to remove — this is the only thing that stops a crash from
# slowly filling the disk with orphaned staging trees.
for entry in "$releases"/.staging.* "$releases"/.archive.*; do
    [ -e "$entry" ] || continue
    entry_base=$(basename "$entry")
    case "$entry_base" in
        .archive.*.tar)
            entry_pid=${entry_base%.tar}
            entry_pid=${entry_pid##*.}
            ;;
        .staging.*) entry_pid=${entry_base##*.} ;;
        *) continue ;;
    esac
    case "$entry_pid" in
        '' | *[!0-9]*) continue ;;
    esac
    if kill -0 "$entry_pid" 2>/dev/null; then
        continue
    fi
    printf '%s: removing stale %s left by dead pid %s\n' "$PROGRAM" "$entry" "$entry_pid" >&2
    rm -rf "$entry"
done

# `*.superseded.*` debris (no leading dot — it's `<sha>.superseded.<pid>`,
# since a moved-aside release directory keeps the sha as its visible name)
# needs different handling from the two patterns above: unlike a
# staging/archive scratch entry, a superseded copy can be the ONLY
# surviving copy of the release `current` points at, if the process that
# moved it aside (the "Already installed?" repair path below) was
# SIGKILLed before the fresh extraction standing in for it was ever
# renamed back into place. Blindly deleting a dead pid's leftover here
# would complete exactly the failure moving it aside was meant to prevent
# in the first place, just delayed to this later run. Restore it if
# nothing has since taken its place; only discard it once something has.
for entry in "$releases"/*.superseded.*; do
    [ -e "$entry" ] || continue
    entry_base=$(basename "$entry")
    entry_pid=${entry_base##*.}
    case "$entry_pid" in
        '' | *[!0-9]*) continue ;;
    esac
    if kill -0 "$entry_pid" 2>/dev/null; then
        continue
    fi
    entry_sha=${entry_base%.superseded.*}
    # Validated the same way the prune loop below validates every release
    # name it acts on — a malformed `.superseded.*` name (foreign debris,
    # not something this script itself ever creates) must not be promoted
    # to an arbitrary path under $releases.
    case "$entry_sha" in
        *[!0-9a-f]*) continue ;;
    esac
    if [ "${#entry_sha}" -ne 40 ]; then
        continue
    fi
    entry_target=$releases/$entry_sha
    # A real, non-symlink directory already at the target means a later
    # run already re-occupied it — the orphan is redundant. Anything else
    # (missing, a symlink, a plain file — adversarial review, round 2:
    # `-e`/`-L` alone treated a dangling symlink or a stray file as
    # "already replaced" and discarded the only surviving copy) is not a
    # real replacement; restore over it rather than discard.
    if [ -d "$entry_target" ] && [ ! -L "$entry_target" ]; then
        printf '%s: removing stale %s left by dead pid %s (superseded by %s)\n' \
            "$PROGRAM" "$entry" "$entry_pid" "$entry_target" >&2
        rm -rf "$entry"
    else
        printf '%s: restoring %s left by dead pid %s — %s was never recreated\n' \
            "$PROGRAM" "$entry" "$entry_pid" "$entry_target" >&2
        rm -rf "$entry_target" 2>/dev/null
        mv "$entry" "$entry_target"
    fi
done

# ---------------------------------------------------------------------------
# Fetch and provenance
# ---------------------------------------------------------------------------

if [ "$do_fetch" -eq 1 ]; then
    # GIT_TERMINAL_PROMPT=0 blocks only git's own terminal prompt; a
    # configured credential helper or GIT_ASKPASS is unaffected by it and
    # can still block waiting for input that will never arrive on this
    # unattended host (security review, round 1). GIT_ASKPASS=/bin/false
    # closes that gap the same way: any askpass invocation fails instantly
    # instead of hanging.
    if ! GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false git -C "$repo" fetch --prune origin; then
        if git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null; then
            printf '%s: warning: git fetch failed; continuing, %s is already mirrored locally\n' \
                "$PROGRAM" "$sha" >&2
        else
            die "git fetch failed and $sha is not present in $repo."
        fi
    fi
fi

git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null ||
    die "$sha does not name a commit in $repo."

full_sha=$(git -C "$repo" rev-parse --verify --quiet "$sha^{commit}") ||
    die "could not resolve $sha to a commit in $repo."

case "$full_sha" in
    *[!0-9a-f]*) die "git rev-parse returned a non-hex object id for $sha" ;;
esac
[ "${#full_sha}" -eq 40 ] || die "git rev-parse did not return a full 40-character object id"

if [ "$allow_unmerged" -eq 0 ]; then
    # `refs/remotes/origin/main`, not the ambiguous short name `origin/main`:
    # gitrevisions(7)'s disambiguation order tries refs/heads/<name> before
    # refs/remotes/<name>, so a *local* branch or tag literally named
    # `origin/main` in this bare mirror would silently shadow the real
    # remote-tracking ref. This mirror only ever gains refs via `git fetch`
    # (never a local `git branch`/`git tag`), so that shadow branch would
    # itself require write access to the mirror to create — but naming the
    # ref unambiguously costs nothing and closes the gap outright.
    if ! git -C "$repo" rev-parse --verify --quiet refs/remotes/origin/main >/dev/null; then
        die "refs/remotes/origin/main does not resolve in $repo — the mirror has no
remote-tracking refs. Provision it with \`git init --bare\` + \`git remote add origin <url>\`
+ \`git fetch\` (see docs/runbooks/deploy.md §1), or pass --allow-unmerged to skip this gate."
    fi
    # Deliberately NOT `git merge-base --is-ancestor` (security-review
    # finding, round 1): that proves only *reachability*, which is a much
    # weaker claim than "CI ran on this tree." This repository merges PRs
    # with merge commits, not squashes/fast-forwards, so a multi-commit
    # PR's individual intermediate commits are ancestors of `main` forever
    # after merge without ever having been a PR head or a `main` tip that
    # CI itself evaluated (`.github/workflows/ci.yml` triggers on
    # `pull_request` — the PR head — and `push: branches: [main]` — main's
    # own tip after merge; neither runs on a commit buried inside a PR's
    # history). `--is-ancestor` would accept any of those silently.
    # `main`'s first-parent history contains exactly the commits CI's
    # push-to-main job evaluated (each merge commit itself, plus any commit
    # pushed to `main` directly) — checking *membership* in that list,
    # rather than mere ancestry, is what actually corresponds to "went
    # through CI a maintainer merged."
    first_parent_shas=$(git -C "$repo" rev-list --first-parent refs/remotes/origin/main) ||
        die "git rev-list failed for refs/remotes/origin/main in $repo — the mirror may be
corrupt."
    if ! printf '%s\n' "$first_parent_shas" | grep -qxF "$full_sha"; then
        die "$full_sha is not on refs/remotes/origin/main's first-parent history — either it
never merged, or it is a commit that was folded into a merge without ever being a PR
head or a \`main\` tip CI itself evaluated. Re-run with --allow-unmerged to deploy it
anyway (hotfix path)."
    fi
else
    # This is the only gate standing between an arbitrary git SHA and code
    # execution as the production Unix user (uv sync's --no-editable forces
    # a wheel build, which runs the archive's own [build-system] backend).
    # Bypassing it must never be silent — security review, round 1.
    printf '%s: WARNING: --allow-unmerged — %s has NOT been verified as having gone through
CI on origin/main. Installing and executing its build/install steps as %s anyway.\n' \
        "$PROGRAM" "$full_sha" "$(id -un)" >&2
fi

release_path=$releases/$full_sha

# ---------------------------------------------------------------------------
# Already installed?
# ---------------------------------------------------------------------------

installed_identity=
if [ -f "$release_path/RELEASE_ID" ]; then
    installed_identity=$(cat "$release_path/RELEASE_ID")
fi

already_installed=0
if [ -d "$release_path" ] && [ ! -L "$release_path" ] &&
    [ -f "$release_path/.venv/bin/finance" ] && [ "$installed_identity" = "$full_sha" ]; then
    # A file existing is not proof the venv actually works: a SIGKILL (the
    # OOM killer, mid-`uv sync`) or an untrapped signal can leave
    # `.venv/bin/finance` present — `uv` writes console scripts late in the
    # install, but not last — while the venv is otherwise incomplete or
    # broken. Trusting the file alone here would let a later `finops
    # deploy` treat this release as installed and run its migration
    # preflight against `finance_prod` before any health check ever runs.
    # Re-run the same verification the fresh-build path below runs; a
    # failure here falls through to a full rebuild exactly like any other
    # incomplete install.
    if _venv_is_usable "$release_path"; then
        already_installed=1
    fi
fi

current_target=
if [ -L "$release_root/current" ]; then
    current_target=$(cd "$release_root/current" 2>/dev/null && basename "$(pwd -P)") ||
        current_target=
fi

if [ "$already_installed" -eq 1 ]; then
    printf '%s is already installed — nothing to do.\n' "$release_path"
else
    if [ -e "$release_path" ] || [ -L "$release_path" ]; then
        # Present but not a complete, verified, identity-matching install.
        # `finops deploy` would refuse it, so nothing new can be *deployed*
        # from it — but if this is the release `current` already points
        # at, something may already be *running* from it, and a plain
        # `rm -rf` would leave `current` dangling with nothing to restart
        # if the rebuild that follows also fails (e.g. the same network
        # hiccup that left it incomplete in the first place — security
        # review, round 1: the venv_building trap above is exactly what
        # can produce this state on the release `current` already names).
        # Move it aside instead of deleting it outright; the EXIT trap
        # restores it if this run doesn't reach a verified install, and it
        # is only removed for good once the rebuild fully succeeds below.
        if [ "$full_sha" = "$current_target" ]; then
            printf '%s: %s is the release `current` points at and is not a complete,
verified install — repairing it without a moment where nothing exists at that path.\n' \
                "$PROGRAM" "$release_path" >&2
            # Checked BEFORE moving anything, independently of the
            # already-installed check above (which short-circuits on a
            # RELEASE_ID mismatch without ever looking at the venv):
            # adversarial review, round 2, found that a release whose
            # venv works perfectly but whose RELEASE_ID merely disagrees
            # with the directory name reaches this exact branch, and the
            # round-1 fix's cleanup() logic would then discard this
            # backup for an unverified fresh attempt if the repair failed
            # — destroying a working release. `superseded_was_verified`
            # is what tells `cleanup()` such a backup must always be
            # restored, never discarded, regardless of what (if anything)
            # occupies `release_path` when this run ends.
            superseded_was_verified=0
            if _venv_is_usable "$release_path"; then
                superseded_was_verified=1
            fi
            superseded=$release_path.superseded.$$
            rm -rf "$superseded"
            mv "$release_path" "$superseded"
        else
            printf '%s: %s exists but is not a complete, verified install — rebuilding it.\n' \
                "$PROGRAM" "$release_path" >&2
            rm -rf "$release_path"
        fi
    fi

    # -----------------------------------------------------------------------
    # Archive, verify, publish
    # -----------------------------------------------------------------------

    staging=$releases/.staging.$full_sha.$$
    tmp_tar=$releases/.archive.$full_sha.$$.tar
    rm -rf "$staging"
    mkdir "$staging"

    # Written to a file rather than piped into tar. This is /bin/sh: there is
    # no `pipefail`, so in `git archive <bad> | tar -x` the pipeline's status
    # is tar's, tar succeeds on empty input, and `set -e` never fires —
    # leaving an empty directory that looks like a release. Checking git's
    # own exit status directly is the only way to catch it here.
    git -C "$repo" archive --format=tar -o "$tmp_tar" "$full_sha" ||
        die "git archive failed for $full_sha."

    tar -xf "$tmp_tar" -C "$staging" || die "extracting the archive of $full_sha failed."
    rm -f "$tmp_tar"
    tmp_tar=

    # `git archive` cannot express a path-traversal or absolute-path tar
    # entry (git tree entries can't be named `.`/`..` or contain `/`, and
    # a single tree can't have both a symlink and a directory of the same
    # name, so the classic "extract a symlink, then write through it"
    # two-entry escape isn't expressible in one archive) — but it CAN emit
    # a symlink entry whose *target* is absolute or contains `../`, which
    # would resolve outside the release tree the first time anything reads
    # through it (a backup, a support bundle, this very manifest check
    # below, which uses `-e` and therefore follows symlinks). This
    # repository tracks no symlinks at all, so refusing any is a correctness
    # no-op for a legitimate release and closes the class outright rather
    # than trying to validate individual targets (security review, round 1).
    if [ -n "$(find "$staging" -type l -print -quit 2>/dev/null)" ]; then
        die "the archive of $full_sha contains a symlink — refusing to install. This
repository tracks no symlinks; one appearing in an archived tree is unexpected and
could resolve outside the release directory once extracted."
    fi

    # Every path a release must have to run at all: pyproject.toml/uv.lock
    # for `uv sync`, alembic.ini/migrations for the preflight `finops deploy`
    # runs, src/finance_app for the package itself. Cheap, and it is the only
    # thing standing between a future stray `.gitattributes export-ignore`
    # rule and a release that extracts cleanly, passes the identity check
    # below, and then fails a `finops deploy` migration preflight with no
    # clue why.
    for required in RELEASE_ID pyproject.toml uv.lock alembic.ini migrations src/finance_app; do
        [ -e "$staging/$required" ] ||
            die "the archive of $full_sha is missing $required — either the archive is
truncated, or .gitattributes has grown an export-ignore rule that drops a path the
release needs at runtime. Nothing was installed."
    done

    archived_identity=$(cat "$staging/RELEASE_ID")
    if [ "$archived_identity" != "$full_sha" ]; then
        # What this actually catches, precisely (security review, round 1,
        # corrected): within this script's own flow `git archive` is always
        # invoked with the same `$full_sha` this compares against, so the
        # "wrong sha's content under this sha's name" scenario
        # docs/deployment.md names — a human running a *different* archive
        # command by hand — isn't something this specific comparison can
        # observe; it would need a corrupted/tampered mirror to trigger
        # here. What it reliably catches: a missing `RELEASE_ID
        # export-subst` rule in `.gitattributes`, which leaves the literal
        # `$Format:%H$` placeholder — the one failure mode genuinely
        # reachable through this script alone, and it's exactly what
        # `ops/identity.py` needs caught before a release with no real
        # identity ever reaches the deploy health gate.
        die "archived RELEASE_ID is '$archived_identity', expected '$full_sha' —
refusing to install a release whose contents disagree with its name."
    fi

    # Atomic: same filesystem, so `releases/<sha>` either does not exist or is
    # a fully extracted tree. Never a half-written one.
    mv "$staging" "$release_path"
    staging=

    # -----------------------------------------------------------------------
    # Build the virtualenv, in place (see the ordering note in the header)
    # -----------------------------------------------------------------------

    printf 'building the release virtualenv in %s ...\n' "$release_path"
    # `venv_building` set *before* the build starts, and cleared only after
    # every check below passes: if this run dies anywhere between here and
    # that clear — `uv sync` itself failing, the console-script check, the
    # editable-install check, a signal — the EXIT trap removes `.venv`
    # rather than leaving one behind. Leaving a `.venv/bin/finance` from a
    # build this script itself judged bad would make `release_is_installed`
    # report the release as installed anyway (it only checks for that one
    # file), which is worse than no venv at all: `finops deploy` would
    # proceed against a release known to be broken instead of refusing it.
    rm -rf "$release_path/.venv"
    venv_building=$release_path/.venv
    # --no-editable: uv installs the root project editable by default, but
    # ops/identity.py documents (and the deploy topology tests assert) a
    # non-editable install — an immutable release should not have its own
    # src/ tree be load-bearing at runtime.
    (cd "$release_path" && uv sync --locked --no-dev --no-editable) ||
        die "uv sync failed in $release_path — .venv has been removed, so
release_is_installed() still reports this release as not installed. Fix the cause (network,
disk space, the lock file) and re-run: this re-extracts and rebuilds the release from
scratch, it does not resume — there is no partial-build state to resume from."

    # Executing the console script, not just testing for its presence: a venv
    # whose entry points were built somewhere else carries a stale
    # absolute-path shebang, and that breakage shows up here and nowhere
    # else.
    "$release_path/.venv/bin/finance" --help >/dev/null 2>&1 ||
        die "$release_path/.venv/bin/finance did not run — the virtualenv is not usable."

    # A direct check of --no-editable's effect, not just trust that the flag
    # worked: ask the release's own interpreter where it actually imports the
    # package from. A non-editable install resolves inside .venv/; an
    # editable one resolves into the release's own src/ tree — exactly what
    # ops/identity.py's design note says must not be true, since that module
    # derives the running release from sys.argv[0] specifically because a
    # non-editable install never resolves near finance_app.__file__.
    imported_from=$("$release_path/.venv/bin/python" -c \
        'import finance_app, pathlib; print(pathlib.Path(finance_app.__file__).resolve())' \
        2>/dev/null) || die "could not import finance_app from $release_path/.venv — the
virtualenv was built but the package is not importable."
    case "$imported_from" in
        "$release_path"/.venv/*) : ;;
        *)
            die "finance_app imports from $imported_from, outside $release_path/.venv —
that is an editable install. ops/identity.py requires --no-editable; refusing to
install a release whose own src/ tree would be load-bearing at runtime." ;;
    esac

    final_identity=$(cat "$release_path/RELEASE_ID")
    [ "$final_identity" = "$full_sha" ] ||
        die "RELEASE_ID changed during the build (now '$final_identity') — refusing to continue."

    # Every post-build check has passed: this venv is no longer provisional,
    # so the EXIT trap must not remove it on a later, unrelated failure (e.g.
    # a prune error after this point).
    venv_building=

    # The repair succeeded — the superseded copy (if this was a repair of
    # the release `current` points at) is no longer needed as a fallback.
    if [ -n "$superseded" ]; then
        rm -rf "$superseded"
        superseded=
    fi

    printf 'installed %s\n' "$release_path"
fi

# ---------------------------------------------------------------------------
# Prune (opt-in) — runs regardless of whether this sha needed building, so
# `release.sh --prune <already-installed-sha>` still reclaims old releases
# rather than silently doing nothing. Reuses `current_target` computed
# above (before this run touched anything) — `current` itself is never
# something this script changes, so it cannot have moved since.
# ---------------------------------------------------------------------------

if [ "$do_prune" -eq 1 ]; then
    kept=0
    # NOT `for name in $(ls -1t "$releases")` (adversarial review, round 2):
    # that unquoted command substitution word-splits its ENTIRE output on
    # IFS before the hex/length validation below ever runs, so foreign
    # debris literally named "<a-real-sha> x" yields a bare "<a-real-sha>"
    # word that passes every check as if it were its own directory — a
    # decoy that makes a genuine release get counted (and later pruned)
    # twice. `find -printf '%T@ %f\n' | sort -rn` plus `${line#* }` (strip
    # only the first space, the numeric mtime field) never re-splits the
    # name itself, however many spaces it contains; the hex/length check
    # then correctly rejects the decoy as too long rather than being
    # fooled into validating a fragment of it. Runs in a subshell (the
    # pipe into `while read`), which is fine here — `kept` only needs to
    # persist across this loop's own iterations, not past it, and the
    # `rm -rf` filesystem effects are real regardless of the subshell.
    find "$releases" -mindepth 1 -maxdepth 1 -printf '%T@ %f\n' 2>/dev/null | sort -rn |
        while IFS= read -r prune_line; do
            name=${prune_line#* }
            case "$name" in
                *[!0-9a-f]*) continue ;;
            esac
            if [ "${#name}" -ne 40 ]; then
                continue
            fi
            kept=$((kept + 1))
            if [ "$kept" -le "$keep" ]; then
                continue
            fi
            if [ "$name" = "$current_target" ]; then
                printf 'keeping %s (current points at it)\n' "$name"
                continue
            fi
            if [ "$name" = "$full_sha" ]; then
                continue
            fi
            printf 'pruning %s\n' "$name"
            rm -rf "$releases/$name"
        done
fi

printf 'Next: FINANCE_ENV_FILE=%s/.env finops deploy %s --release-root %s\n' \
    "$release_root" "$full_sha" "$release_root"
