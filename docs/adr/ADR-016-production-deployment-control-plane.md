# ADR-016: Production deployment control plane — Compose interpolation, the long-running-service question, and release-promotion concurrency

**Status:** Accepted

Refines ADR-007, ADR-008, ADR-011 and ADR-015. Supersedes none of them: every invariant those ADRs assert still holds, and two of them (ADR-008's "rollback is one step", ADR-011's "no in-application scheduler") are strengthened here. This ADR resolves a set of Milestone 7 defects that two independent review rounds (security-reviewer, qa-adversarial) each traced to one structural collision rather than to individual bugs.

## Context

Milestone 7 split `deploy/compose.yaml` into least-privilege services — `postgres`, long-running `app`, and one-shot `migrate` / `sync` / `backup` / `finops` / `deploy` — so that no single container's `environment:` holds every production credential. The `deploy` service is the only one with `/var/run/docker.sock` mounted, because `finops deploy` / `rollback` / `restart` drive `docker compose` themselves; it was therefore given the narrowest credential set in the file.

That split collides with how Compose works. **`docker compose` interpolates every `${VAR:?required}` in the whole file before it runs anything**, regardless of which service or profile was selected. So `finops deploy`, running inside the `deploy` container with only `CONTAINER_IMAGE_REPO` / `DATABASE_URL` / `OBSERVER_DATABASE_URL`, dies at its first `docker compose pull app`:

```
error while interpolating services.app.environment.AGENT_DATABASE_URL:
required variable FINANCE_AGENT_DB_PASSWORD is missing a value
```

Reproduced end to end against the real `docker compose` binary by two reviewers independently (`tests/unit/test_deploy_topology_regression.py`). The release mechanism cannot run in the documented production topology at all. The same whole-file interpolation is why every `finance-*.service` unit currently has to `LoadCredentialEncrypted=` almost the entire credential set — the periodic *health check* unit has to decrypt the **Plaid production access token** to run a read-only query.

The obvious fix — give the `deploy` container every credential — was rejected: it deletes the reason the split exists. But five more defects sit in the same neighbourhood and cannot be settled independently of it:

1. `app`'s command is `["finance", "status"]`, which exits immediately; with `restart: unless-stopped` the container crash-loops forever on the production VPS.
2. The post-deploy probe is `docker compose exec -T app true`, which passed *while* `app` was crash-looping (it caught an up-window).
3. `migration_status` resolves `alembic.ini` from the process CWD — `/opt/finance-app`, a bind-mounted host checkout — not from the image being deployed.
4. `mark_healthy` converts advisory-lock contention into a silent `failed` row while `finops deploy` still exits 0, after `docker compose up -d` has already switched the host.
5. Migration `0005`'s partial unique index on `ops.releases(status)` cannot apply to a database that already has duplicate `current` rows — i.e. exactly the database that hit the race the index prevents.
6. `finance-restore-drill.service`'s `PrivateTmp=true` breaks `restore-verify.sh`'s host-side `mktemp` file, which the Docker **daemon** resolves outside the unit's private `/tmp` namespace, so the weekly restore drill fails every week.

### The observation that dissolves the collision

**Docker-daemon access is a privilege ceiling.** Any process that can talk to `/var/run/docker.sock` — the `deploy` container, and equally every host-side `finance-*.service` unit that shells out to `docker compose` — can `docker run -v /:/host`, read `/run/credentials/*` (systemd's decrypted credential tmpfs of any running unit), `docker inspect` every other container's environment, and reach the host key that `systemd-creds` decryption depends on. Withholding `FINANCE_APP_DB_PASSWORD` from a process that already holds the socket buys nothing against a compromise of that process.

So credential scoping at the *invoker* level is defence-in-depth against accident — a stray log line, a `docker inspect` by a less-privileged reader, a bug that echoes the environment — not a containment boundary. The boundary that is real, and that this ADR protects unchanged, is **which credentials are materialised into a container's environment, and for how long** — above all for any container that is long-lived and processes attacker-influenceable input (Plaid merchant text, model responses).

Once that is stated plainly, the resolution follows: stop trying to make Compose's whole-file `:?` assertion express a per-job requirement it structurally cannot express, and stop keeping a long-running container that has no work to do.

No new service, framework, or dependency is introduced by this ADR. Everything below is PostgreSQL, systemd, Docker Compose, and plain Python — the three-question test in CLAUDE.md is answered "nothing new is needed" in all seven decisions.

## Decision

### D1 — Compose stops enforcing credential requirements; the job wrapper does

`deploy/compose.yaml` keeps every service in one file (handoff §11) and **removes every `${VAR:?required}`**, replacing them with `${VAR:-}`. The file must parse and interpolate cleanly against an empty environment. `:?` is a whole-file assertion; our requirements are per-job; it is the wrong tool and it is the direct cause of the failure.

Requirement enforcement moves to `deploy/scripts/with-production-env.sh`, which gains a job argument:

```
with-production-env.sh <job> -- <command...>
    job ∈ { app | migrate | sync | backup | finops | deploy | health | restore-drill }
```

The script owns one table mapping job → required credential set, and for the named job:

- exports **only** that job's variables (not every credential that happens to be in `$CREDENTIALS_DIRECTORY`);
- **fails loudly, naming the missing credential and the job**, if any required one is absent;
- rejects an **empty** value for a required credential as hard as a missing one.

The credential matrix, which is the contract this ADR fixes:

| job | required |
|---|---|
| `app` | `FINANCE_APP_DB_PASSWORD`, `FINANCE_AGENT_DB_PASSWORD`, active provider key (`OPENAI_API_KEY` \| `ANTHROPIC_API_KEY`) |
| `migrate` | `FINANCE_MIGRATOR_DB_PASSWORD` |
| `sync` | `FINANCE_APP_DB_PASSWORD`, `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ACCESS_TOKEN` |
| `backup` / `restore-drill` | `FINANCE_BACKUP_DB_PASSWORD`, `FINANCE_APP_DB_PASSWORD`, `BACKUP_ENCRYPTION_KEY` |
| `finops` | `FINANCE_OBSERVER_DB_PASSWORD` |
| `health` | `FINANCE_APP_DB_PASSWORD` |
| `deploy` | `FINANCE_MIGRATOR_DB_PASSWORD`, `FINANCE_APP_DB_PASSWORD`, `FINANCE_OBSERVER_DB_PASSWORD` |

Each unit's `LoadCredentialEncrypted=` list is trimmed to exactly its job's set. The health timer no longer decrypts the Plaid access token. The sync timer no longer decrypts the backup encryption key or the migrator password. The `deploy` control plane holds **three database passwords and zero API keys** — never the Plaid production access token, never `BACKUP_ENCRYPTION_KEY`, never a model provider key. That reduction is only achievable because of D2.

Two consequences of removing `:?` must be handled explicitly, because Compose's assertion was doing a small amount of real work:

- **Empty passwords must fail loudly somewhere.** An empty DSN password fails at SCRAM authentication — loud, not silent. The one genuinely silent case is `BACKUP_ENCRYPTION_KEY`: `gpg --symmetric` with an empty passphrase *succeeds* and produces an archive anyone can decrypt. That hazard exists in the file **today** (`${BACKUP_ENCRYPTION_KEY:-}` is already optional), which is itself proof that `:?` was never the right protection. `finance_app.ops.backup` must refuse an empty passphrase before invoking `gpg`, and the job matrix marks it required.
- **A drift test replaces the compile-time check.** A unit test parses `deploy/compose.yaml` and `with-production-env.sh` and asserts: no `:?` remains; every `${VAR}` referenced by a service appears in that service's job set; the `deploy` job's set is a superset of the union of the variables used by `postgres`, `migrate`, and the `app` service's *selfcheck-relevant* keys; and the `deploy` set excludes `PLAID_*`, `FINANCE_BACKUP_DB_PASSWORD`, `BACKUP_ENCRYPTION_KEY` and both provider keys. This assertion is the mechanical guard that keeps the two files in sync — it is the thing that must never be deleted.

**Pin the Compose project name.** `deploy/compose.yaml` gains a top-level `name: finance-app`. Today the project name is derived from the compose file's parent directory and happens to match between the host (`/opt/finance-app/deploy`) and the `deploy` container (`/opt/finance-app/deploy`, identical bind path). That is luck. A project-name mismatch would make `finops deploy` create a *second parallel stack* instead of updating the running one — a silent split-brain with two Postgres containers. Pin it.

### D2 — There is no long-running `app` service until Milestone 8

`app` stops being long-running. It loses `restart: unless-stopped` and its `command:`, gains `profiles: ["app"]`, and is invoked only as `docker compose --profile app run --rm app <cmd>`.

The honest position: in the v1 topology **there is no long-running application process**. Sync, backup, and health are systemd timers by ADR-011. Analytics and the CLI are on demand. `finance chat` is interactive. The webhook server is Milestone 8. A container kept alive by `finance status`, `sleep infinity`, or any other idle placeholder is not a service — it is a health signal that reports "up" for a process that does nothing, which is precisely how the crash loop hid behind a passing probe. Inventing a process so a probe has something to poke is backwards.

Downstream:

- `docker compose up -d` brings up `postgres` only (plus `caddy` under the `webhook` profile once Milestone 8 lands).
- `finops restart` is redefined from "restart the app container" to **"converge the running stack to the currently recorded release"** — `docker compose up -d` with `RELEASE_ID` set. Today that touches `postgres` only and is a no-op when already converged; it reports what it changed. The command name and its handoff §10 contract survive Milestone 8 unchanged, when `app` becomes a genuine long-running service again.
- `finance chat` and other interactive use run via `systemd-run --pty` + `with-production-env.sh app` + `docker compose --profile app run --rm app finance chat`, per the pattern already documented in `docs/runbooks/deploy.md §2.5`. This costs a wrapper and a container cold start. It buys something real: **no container holds a model provider API key or a database password 24 hours a day**, where any process with docker access could read it out of `docker inspect`.

**Revisit trigger, stated now so it is not re-derived later:** Milestone 8's webhook endpoint. At that point `app` becomes long-running again with a real command (`finance serve-webhook`), a real Compose `healthcheck:`, and a real HTTP liveness probe — and D3's smoke check becomes a complement to that probe, not a substitute for it.

### D3 — The post-deploy probe runs the image being deployed

`probe_application`'s `docker compose exec -T app true` is deleted. It answered "is some container up right now", which is neither the release's health nor even a fixed target once D2 lands.

The gate becomes a **one-shot smoke run of the exact image under deployment**:

```python
# src/finance_app/ops/status.py
def probe_release(
    *,
    release_id: str,
    compose_file: str = DEFAULT_COMPOSE_FILE,
    run_compose_fn: Runner = _run_compose,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """`--profile app run --rm -T app finance selfcheck --json`, with
    RELEASE_ID pinned to `release_id`. Returns
    {"status": "healthy"|"unhealthy"|"wrong_image"|"unreachable",
     "reported_release_id": str | None, "detail": ...}."""
```

backed by a new deterministic command in the application image:

```python
# src/finance_app/ops/selfcheck.py
def selfcheck() -> dict[str, Any]:
    """{"release_id", "app_version", "database", "migrations": {...}, "overall"}.
    Opens a finance_app session, SELECT 1, compares the DB's applied
    Alembic revision against this image's packaged head (D4).
    Never calls a model provider; never needs an API key."""

# src/finance_app/cli/main.py
@app.command()
def selfcheck(json_output: bool = typer.Option(False, "--json")) -> None:  # exit 0 iff healthy
```

`probe_release` fails the deploy if the container exits non-zero, if the JSON is unparseable, if `overall != "healthy"`, **or if `reported_release_id != release_id`** — the last catching a wrong/stale image, which no `exec`-based probe could ever detect.

`deploy_health_check` becomes:

```python
def deploy_health_check(
    session: Session, *, release_id: str,
    compose_file: str = DEFAULT_COMPOSE_FILE, run_compose_fn: Runner = _run_compose,
) -> dict[str, Any]
```

= `db_status(session)` (observer-role reachability, kept so the operator can distinguish "database down" from "image broken") **+** `probe_release(...)`. It no longer calls `migration_status` from the deploy container; migration state now comes back from inside the release image, which is the only process that knows what head that release expects. QA-14's narrowness rule is unchanged: sync staleness and backup verification stay out of the deploy gate.

`run_compose` gains a `timeout: float | None` parameter and passes it to `subprocess.run`, so a hung `docker compose` cannot hang a deploy indefinitely — a `subprocess.TimeoutExpired` surfaces as `ComposeError`.

`_application_liveness()` in `aggregate_health` is unaffected (it is an in-process signal for the health timer, correctly documented as weaker).

### D4 — The migration head is a property of the installed package, never of the CWD

Two parts, because CWD was only half the problem.

**(i) The release image is the authoritative reporter.** The `deploy` container is pinned to `${RELEASE_ID:-latest}` and was started *before* the new release id was known — it runs the **old** image. So even a perfectly CWD-independent lookup inside the deploy container would report the previous release's expected head. Only the image being deployed can answer "what head does this release expect", and D3's `finance selfcheck` is how it answers.

**(ii) Inside any image, resolution is package-relative.** `migrations/` moves to `src/finance_app/migrations/` (shipped inside the wheel), and runtime code builds its Alembic config programmatically:

```python
# src/finance_app/db/alembic_config.py
class PackagedMigrationsUnavailableError(RuntimeError): ...

def packaged_alembic_config() -> alembic.config.Config:
    """script_location = importlib.resources.files("finance_app") / "migrations"."""

def packaged_head_revision() -> str:
    """Raises PackagedMigrationsUnavailableError rather than returning None."""

# src/finance_app/db/migrate.py
def upgrade_to_head() -> str:
    """`alembic upgrade head` against ALEMBIC_DATABASE_URL, from any CWD.
    Returns the revision now applied."""
```

`status.migration_status(session)` loses its `alembic_ini_path` parameter and calls `packaged_head_revision()`. Its current "any exception → `head = None` → status `unknown`" fallback is removed: with a packaged lookup, an image that cannot report its own head is broken, and the deploy gate must say so rather than degrade to a status that reads like a soft warning.

The root `alembic.ini` stays for authoring (`alembic revision`, developer `alembic upgrade`), with `script_location = %(here)s/src/finance_app/migrations`. The `migrate` service switches to `["python", "-m", "finance_app.db.migrate", "upgrade"]`, removing the last CWD dependency in the release path.

**Flagged as the risk of this decision:** moving the directory silently breaks path-matching guards unless they move with it — `.claude/hooks/guard-applied-migrations.sh` (the `migrations/versions/*.py` glob that mechanically enforces "never rewrite an applied migration"), `pyproject.toml`'s per-file lint ignores, `docs/database.md`, and the CI migration jobs. A test must assert the hook's glob matches the real migrations directory, so this protection cannot rot. CI's "apply the previously-published release's migrations first" step must keep invoking `alembic upgrade head` for the *previous* image (which predates this change) while using the new entrypoint for the new one.

### D5 — One lock for the whole deploy, taken before anything touches the host; contention aborts

The advisory lock is in the wrong place and has the wrong failure behaviour. It is taken at the *end*, around promotion, which is the least dangerous moment; and on contention `mark_healthy` silently writes `failed` while `finops deploy` exits 0 — after `docker compose up -d` has already switched the host. Bookkeeping and reality disagree, and the exit code endorses the wrong one.

The dangerous window is not two promotions racing; it is **two deploys racing to change the host**, whose winner is decided nondeterministically by Docker. So the lock must cover the entire operation and be acquired before the first host mutation.

```python
# src/finance_app/ops/release.py
class DeployInProgressError(RuntimeError): ...   # another deploy/rollback holds the lock
class DeployLockLostError(RuntimeError): ...     # we held it and no longer do

@contextmanager
def deploy_lock(engine: Engine) -> Iterator[Connection]:
    """pg_try_advisory_lock(_DEPLOY_PROMOTION_LOCK_KEY) on a dedicated
    connection held for the whole deploy. Raises DeployInProgressError
    immediately if not acquired. Released when the connection closes —
    including when the process is killed, which is why this is a session
    lock (pg_try_advisory_lock) and not a transaction lock."""

def assert_lock_held(conn: Connection) -> None:
    """Re-verify via pg_locks against pg_backend_pid(); raise
    DeployLockLostError otherwise. Called immediately before promotion."""
```

Behaviour on contention: **fail fast, change nothing, exit non-zero.** No retry, bounded or otherwise. On a single-user VPS a concurrent deploy is always an operator mistake or a duplicated automation trigger; a bounded retry only delays that diagnosis and risks two deploys interleaving at the retry boundary. The message names the condition ("another deploy or rollback is in progress; refusing to start") and the operator re-runs.

`mark_healthy` no longer swallows contention. It takes the held connection, calls `assert_lock_held`, and raises `DeployLockLostError` if the lock is gone. `finops rollback` takes the same lock with the same semantics — a rollback racing a deploy is the identical hazard.

The deploy state machine, explicit:

```
ACQUIRE_LOCK ──contended──> ABORT                  exit 1; host unchanged; no release row
     │
     ▼
START_DEPLOY (insert status='pending', capture replaces_release_id)
     │
     ▼
PULL ──fail──> MARK_FAILED ──> ABORT               exit 1; host unchanged
     │
     ▼
MIGRATE ──fail──> MARK_FAILED ──> ESCALATE         exit 1; host unchanged, schema may be
     │                                              partially advanced — no automatic
     │                                              downgrade (Class B/C, see below)
     ▼
CONVERGE (docker compose up -d)                     ← first host mutation
     │
     ▼
SMOKE (probe_release on the new image)
     │                          └──unhealthy──> MARK_FAILED ──> AUTO_ROLLBACK ──> exit 1
     ▼
ASSERT_LOCK_STILL_HELD ──lost──> MARK_FAILED + ESCALATE, *no* auto-rollback ──> exit 1
     │
     ▼
PROMOTE (status='current') ──> PERSIST RELEASE POINTER (D7) ──> exit 0
```

`ops.releases.status` transitions (unchanged set, now the only legal ones):

```
pending  -> current      promotion; requires the deploy lock verifiably held
pending  -> failed       any preflight/health failure, lost lock, or stale-pending reap
current  -> previous     superseded by a newer successful deploy
current  -> rolled_back  an explicit rollback promoted a different release
previous -> current      rollback target promoted
previous -> history      superseded, or same release_id as the retained current
```

Two rules fall out and must be implemented:

- **`finops deploy`'s exit status and its printed outcome are derived from a re-read of the release row after the final transaction**, never from an in-memory flag. This structurally eliminates the class of bug where the process reports success and the row says `failed`.
- **Lock lost mid-deploy does not trigger auto-rollback.** We do not know what the other actor did; rolling back on top of it could make things worse. Stop, record, escalate — CLAUDE.md Class C's "refusing an unsafe mutation is the correct autonomous action". The same applies to a failed migration: Alembic cannot be assumed to safely downgrade a partially-applied migration, so the deploy aborts before touching the host and escalates rather than attempting schema repair.

The partial unique index from D6 remains as the last-resort invariant. The stale-`pending` reaper stays (a killed process now also drops the lock, but the row still needs resolving).

### D6 — Migration 0005 deduplicates before it constrains

Migration `0005_a1c3e9f4d2b7_release_rollback_safety.py` is amended in place — legitimately, because it has never been applied to any durable database (production does not exist yet; only CI and disposable dev databases have seen it). **If that ever becomes uncertain, the correct action is a new `0006`, not an edit.** CLAUDE.md's prohibition is on rewriting *applied* migrations.

`upgrade()` gains a dedup step before `create_index`:

- Among `status = 'current'` rows, retain the one with the greatest `(deployed_at, id)`. It is the release the host actually last converged to, because `docker compose up -d` is last-writer-wins.
- Demote the rest to `status = 'history'` — not `failed` (they may have been genuinely healthy when current) and not `previous` (we have no evidence they were the known-good predecessor). `history` is the honest description: "was current, superseded".
- Apply the same rule to `status = 'previous'`.
- Then, if the retained `previous` has the same `release_id` as the retained `current`, demote it to `history` as well — otherwise `finops rollback` becomes a silent no-op that redeploys the identical image (the QA-5 hazard, reachable through this data path).
- Every demoted row gets a `notes` breadcrumb recording the dedup and the migration that did it.

**No row is ever deleted.** Provenance survives; only `status` and `notes` change.

`downgrade()` drops the index and the column but does **not** restore duplicate `current` rows. State that in the migration docstring: the pre-dedup state was invalid, and re-creating an invalid state is not a service to anyone.

### D7 — Throwaway secrets for host-launched containers live in `RuntimeDirectory`, and directories get mounted, not files

`finance-restore-drill.service` keeps `PrivateTmp=true` and gains:

```
RuntimeDirectory=finance-app-restore-drill
RuntimeDirectoryMode=0700
```

`PrivateTmp=` namespaces `/tmp` and `/var/tmp` only; `/run/<RuntimeDirectory>` is created in the **host** mount namespace, so the Docker daemon can resolve it, and systemd removes it when the unit stops. `restore-verify.sh` stages its scratch password at `$RUNTIME_DIRECTORY/scratch_password` and mounts the **directory**:

```
-v "$RUNTIME_DIRECTORY:/run/scratch:ro"   POSTGRES_PASSWORD_FILE=/run/scratch/scratch_password
```

Directory bind mounts are the robust case; single-file bind mounts additionally break on inode replacement and are the shape that produced this defect.

The script must **fail loudly rather than silently fall back to `/tmp`** when running under systemd: if `$RUNTIME_DIRECTORY` is unset while `$CREDENTIALS_DIRECTORY` is set, error out naming the missing `RuntimeDirectory=`. A `mktemp -d` fallback is retained only for a manual, non-systemd run.

This is the general rule, not a one-off: **any host-side script that hands a file to a container it launches must stage that file somewhere the Docker daemon can see — `RuntimeDirectory`, never `/tmp` — and mount the containing directory.** A regression test asserts no unit combines `PrivateTmp=true` with a script that bind-mounts a `/tmp` path.

### D8 — The recorded release must be persisted where the timers read it

Not on the original list, but it is the same defect class and it makes rollback and provenance wrong, so it is in scope.

`finops deploy` sets `RELEASE_ID` only in the environment of its own `docker compose` child processes. Nothing writes it back to `/etc/finance-app/env`, which is what every `finance-*.service` unit reads via `EnvironmentFile=`. **After a successful deploy, the sync, backup, and health timers keep running the previous image — or `latest`.** Release identity silently diverges between the release path and the scheduled path, which is a direct contradiction of ADR-008's "what is running is always answerable".

Decision: the **host-side wrapper** persists the pointer, not the container.

- `finops` gains a read-only command `current-release --json` (observer role, `status.current_release`).
- `deploy/scripts/finops.sh`, after any `deploy` / `rollback` invocation regardless of its exit status, queries `finops current-release --json` and atomically rewrites `RELEASE_ID=` in `/etc/finance-app/env` (write to a temp file in the same directory, `chmod 0644`, `mv`). Because it re-reads the database rather than echoing what it was asked to deploy, it converges to the truth even after a partial failure or an auto-rollback.
- The deploy unit needs `ReadWritePaths=/etc/finance-app` alongside `ProtectSystem=strict`.

Rejected alternative: bind-mounting `/etc/finance-app/env` read-write into the `deploy` container. It is simpler in control flow and (given the privilege ceiling) no worse in security, but it puts host configuration writes inside the container, and the host wrapper already exists.

## Consequences

### What we accept

- **The `deploy` container still holds three database passwords**, including the DDL-capable `finance_migrator` DSN. There is no way around it: whoever runs `docker compose up`/`run migrate` must be able to interpolate what those services need. The honest framing is that this is not a boundary at all — socket access already subsumes it — and the ADR says so, so nobody re-derives false comfort from the narrow list. What the split *does* buy, and keeps: the release control plane never touches the Plaid production access token, the backup decryption key, or a model provider key.
- **Compose no longer refuses to start on a missing credential.** That check moves to `with-production-env.sh` and to the drift test. It is better than what it replaces (per-job, names the credential, catches the empty-`BACKUP_ENCRYPTION_KEY` case Compose never caught) but it is *our* code now, and the drift test is what keeps it honest.
- **Interactive `finance chat` costs a `systemd-run` wrapper and a container start.** Accepted in exchange for no long-lived container holding a provider key.
- **The topology temporarily has no long-running application service.** `docker compose ps` on the VPS shows Postgres and nothing else, which will look wrong to anyone who has not read this ADR. Milestone 8 restores it with a real process.
- **Moving `migrations/` touches guard rails.** Called out in D4 as the sharpest risk in this change set.

### What gets better

- The release mechanism works at all, in the documented topology.
- No crash loop, and no probe that passes by catching an up-window.
- The post-deploy gate tests the image being deployed, including that it *is* the image that was asked for.
- Deploy concurrency has one lock covering the whole operation, auto-released on process death, with a loud abort instead of a silent divergence.
- Release identity is consistent between `finops`, `ops.releases`, and the systemd timers.
- The weekly restore drill runs, so `finops backup-status` stops reporting `unverified` forever and `aggregate_health` stops reporting the whole system unhealthy for the wrong reason.

### Implementation plan

Ordered so each step is independently testable. Class B throughout (migrations, credential handling, release semantics): implementation, then separate specialist review, adversarial tests, and security review — no self-approval.

1. **`deploy/compose.yaml`** — add top-level `name: finance-app`; replace every `${VAR:?...}` with `${VAR:-}`; give `app` `profiles: ["app"]` and remove `restart:` and `command:`; extend the `deploy` service's `environment:` to `FINANCE_MIGRATOR_DB_PASSWORD`, `FINANCE_APP_DB_PASSWORD`, `FINANCE_OBSERVER_DB_PASSWORD` (as the DSNs it needs) and nothing else; update the file header comment to describe D1/D2 rather than the superseded rationale.
2. **`deploy/scripts/with-production-env.sh`** — add the `<job> -- <cmd>` interface and the D1 matrix; export only the job's set; fail on missing *or empty* required values.
3. **`deploy/systemd/*.service`** — trim each `LoadCredentialEncrypted=` to its job's set; pass the job name through the `ExecStart` wrapper; `finance-app.service`'s `ExecReload` becomes `up -d` (converge) rather than `restart app`; `finance-restore-drill.service` gains `RuntimeDirectory=`/`RuntimeDirectoryMode=`; the deploy path gains `ReadWritePaths=/etc/finance-app`.
4. **`deploy/scripts/restore-verify.sh`** — stage the scratch password in `$RUNTIME_DIRECTORY`, mount the directory read-only, error out when `$RUNTIME_DIRECTORY` is unset under systemd.
5. **`deploy/scripts/finops.sh`** — persist `RELEASE_ID` into `/etc/finance-app/env` from `finops current-release --json`, atomically, after every deploy/rollback.
6. **`src/finance_app/db/alembic_config.py`, `src/finance_app/db/migrate.py`** (new) and the `migrations/` → `src/finance_app/migrations/` move; update `alembic.ini`, `pyproject.toml` per-file-ignores, `.claude/hooks/guard-applied-migrations.sh`, `.github/workflows/ci.yml`, `docs/database.md`.
7. **`src/finance_app/ops/selfcheck.py`** (new) and `finance selfcheck --json` in `cli/main.py`.
8. **`src/finance_app/ops/status.py`** — delete `probe_application`; add `probe_release`; rework `deploy_health_check` to take `release_id` and consume the selfcheck payload; `migration_status` drops `alembic_ini_path` and uses `packaged_head_revision()`, with the `unknown` fallback removed.
9. **`src/finance_app/ops/compose.py`** — add `timeout`; resolve `DEFAULT_COMPOSE_FILE` from `$COMPOSE_FILE` when set.
10. **`src/finance_app/ops/release.py`** — `deploy_lock`, `assert_lock_held`, `DeployInProgressError`, `DeployLockLostError`; `mark_healthy` stops converting contention into `failed`.
11. **`src/finance_app/cli/finops.py`** — wrap `deploy`/`rollback` in `deploy_lock`; implement the D5 state machine; derive exit status from a re-read of the release row; add `current-release`; redefine `restart` as converge.
12. **`migrations/versions/0005_...`** — add the dedup step ahead of `create_index`; document the one-way `downgrade()`.
13. **`src/finance_app/ops/backup.py`** — refuse an empty `BACKUP_ENCRYPTION_KEY`.
14. **Docs** — `docs/deployment.md` (topology, release sequence, health gate), `docs/runbooks/deploy.md` (per-job `systemd-run` invocations, `finance chat`, restart semantics), `docs/backups.md` (restore-drill mechanics), `docs/architecture.md` (deployment section), `docs/database.md` (migrations location). Recommend a one-line annotation to handoff §20 noting that `app` is one-shot until Milestone 8, referencing this ADR.

### Tests the implementation pass must add

No existing test is weakened. The `xfail(strict=True)` markers in `tests/unit/test_deploy_topology_regression.py` are deleted as each defect is fixed — the tests stay. `test_deploy_env_is_sufficient_for_compose_interpolation` keeps passing; its required set narrows to the D1 `deploy` job set.

- Compose/wrapper drift test (D1): no `:?` remains; per-service variables ⊆ that job's set; `deploy`'s set excludes `PLAID_*`, `FINANCE_BACKUP_DB_PASSWORD`, `BACKUP_ENCRYPTION_KEY`, provider keys; `deploy`'s set ⊇ what `postgres` + `migrate` + selfcheck need.
- `with-production-env.sh` fails on a missing *and* on an empty required credential, naming both job and credential.
- Compose project name is pinned (`name: finance-app`).
- No service with a `restart:` policy has a one-shot command (existing test, un-`xfail`ed).
- `probe_release` returns `wrong_image` when the selfcheck reports a different `release_id`; `unreachable` on `ComposeError`; `unhealthy` on a non-zero exit or unhealthy payload; and honours `timeout`.
- `packaged_head_revision()` resolves with an arbitrary CWD and with `migrations/` absent from the CWD; `.claude/hooks/guard-applied-migrations.sh`'s glob matches the real migrations path.
- Integration: two concurrent `finops deploy` invocations — the second raises `DeployInProgressError`, writes **no** release row, and issues **no** `docker compose` call (assert on the injected runner).
- Integration: lock lost between converge and promote → release `failed`, non-zero exit, **no** auto-rollback attempted.
- Integration: `finops deploy` exit status always agrees with the final `ops.releases` row.
- Migration: 0005 applies cleanly to a database seeded with two `current` rows and two `previous` rows; the most recent of each survives; the rest become `history` with notes; no row is deleted; a `previous` sharing the retained `current`'s `release_id` is demoted.
- Restore drill: no systemd unit combines `PrivateTmp=true` with a script that bind-mounts a `/tmp` path; `restore-verify.sh` errors when `$RUNTIME_DIRECTORY` is unset under systemd.
- `finops.sh` writes `RELEASE_ID` matching `finops current-release` after a deploy and after an auto-rollback.

## Revisit when

- **Milestone 8 lands a webhook server.** `app` becomes long-running with a real command and a Compose `healthcheck:`; D3's smoke check becomes a complement to an HTTP liveness probe rather than the whole gate; `finops restart` regains a container to restart without changing its contract.
- **A second host or a second operator appears.** The privilege-ceiling argument in the Context is specific to one VPS where every Docker-capable process is root-equivalent. Split hosts or multiple keyholders would make invoker-level credential scoping a real boundary again, and would justify revisiting both the single-compose-file decision (handoff §11) and D1's matrix.
- **Compose gains per-service interpolation scoping**, i.e. the ability to parse and run one service without resolving variables belonging to others. That is the upstream feature whose absence forced D1; if it appears, `:?` becomes the right tool again and the wrapper's matrix could shrink to a cross-check.
- **A deploy is ever observed contending for the lock in normal operation.** That would mean the "concurrent deploy is always operator error" premise is wrong, and the fail-fast decision in D5 should be reconsidered — with evidence, not in anticipation.
