# ADR-015: Backup encryption via GPG symmetric encryption, keyed by a single systemd credential

**Status:** Accepted

## Context

Handoff §22 requires backups to be encrypted before anything leaves the VPS, and ADR-010 requires the key to live only as a systemd encrypted credential — never in the repository, CI, or a Claude Code session. Something has to actually perform that encryption inside `deploy/scripts/backup.sh`/`src/finance_app/ops/backup.py`, and it needs to run unattended on a timer with no human present to type a passphrase interactively.

Two credible tools were considered:

- **`age`** — modern, minimal, a single well-reviewed Go binary, increasingly the default recommendation for new projects. Not present on Debian bookworm's default `apt` repository set (would need a third-party repo or a static binary fetch) and not preinstalled on the macOS development machine used to build this milestone.
- **GPG (`gnupg`)** — ubiquitous, already a Debian/Ubuntu base-image package, supports fully unattended symmetric encryption (`--batch --passphrase-fd 0 --symmetric --cipher-algo AES256`) with no keypair management. Mature, if less minimal than `age`.

CLAUDE.md's dependency question applies directly here: *what concrete problem does a new tool solve that Docker/systemd/plain Python can't solve more simply, and what new failure mode does it introduce?* Adding `age` would mean a third-party APT repository (an extra supply-chain trust decision, similar in kind to the PGDG repository the Dockerfile already adds for `postgresql-client-17`) purely to get symmetric passphrase encryption GPG already provides out of the box.

## Decision

Encrypt backups with GPG symmetric encryption (`--cipher-algo AES256`), passphrase-only — no keypair, no GPG keyring management. The passphrase is a single value, `BACKUP_ENCRYPTION_KEY`, minted once via `systemd-creds encrypt` and delivered to `finance-backup.service`/`finance-restore-drill.service` as `LoadCredentialEncrypted=backup_encryption_key` (ADR-010). It is passed to `gpg` via `--passphrase-fd 0` (stdin), never as a command-line argument, so it never appears in `ps`, shell history, or a subprocess error message (`src/finance_app/ops/backup.py::_run`).

`gnupg` is installed in the runtime image (`Dockerfile`) alongside `postgresql-client-17`; both are already-necessary, already-vetted Debian/PGDG packages, not a new third-party dependency.

## Consequences

- One passphrase to rotate (docs/runbooks/deploy.md's rotation procedure), not a keypair — simpler operational surface, at the cost of no public-key workflow (irrelevant here: this is single-VPS, single-operator, symmetric-by-nature).
- Losing `BACKUP_ENCRYPTION_KEY` makes every existing backup unrecoverable. The restore-verification drill (`finance-restore-drill.timer`, weekly) is what catches this early — a backup that decrypts and restores cleanly this week proves the current key still works, not just that a file exists.
- `gpg --symmetric` output is not authenticated the way an AEAD scheme is; an attacker who can write to the backup volume could tamper with a `.gpg` file without a signature check catching it before decryption is attempted. Mitigated in practice by `pg_restore` and the restore-verification sanity checks failing loudly on a corrupted/tampered dump — not a silent-corruption risk — but this is real enough that the security reviewer should confirm it's an acceptable residual risk for a single-user system rather than something needing GPG's `--sign`/AEAD-mode considerations.
- Rotating the passphrase does not need to re-encrypt old backups — each archive was encrypted with whatever key was active at the time; keep the old key available (out of band, owner's responsibility) for as long as any backup encrypted under it is still within the retention window (docs/backups.md).

## Revisit when

The backup destination gains multiple independent operators/keyholders (asymmetric encryption would then earn its complexity), or `age` becomes a first-class Debian/PGDG package removing the third-party-repository cost that ruled it out here.
