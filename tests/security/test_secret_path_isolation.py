"""Proves docs/security-model.md invariant 4's "tested, not asserted"
requirement that production secret paths are not mounted into dev or CI.

Static, config-file-level tests deliberately -- they do not need a running
database or container, so they run everywhere (including the same
environment an autonomous engineering agent operates in) and fail loudly
the moment a production credential path is copy-pasted into a
dev/CI-facing file, which is exactly how this class of leak tends to
happen in practice (someone adapting a systemd unit or compose service
for local use and not noticing they carried the production credential
directory along with it).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Anything that would route a dev/CI process at the VPS's real,
# systemd-decrypted production credential material. Matched as plain
# substrings -- deliberately conservative, see module docstring.
_PRODUCTION_SECRET_MARKERS = (
    "/etc/finance-app/credentials",
    "LoadCredentialEncrypted",
    "CREDENTIALS_DIRECTORY",
)

# Files a dev/CI workflow actually executes against. Excludes
# deploy/systemd/*.service and deploy/scripts/with-production-env.sh
# deliberately -- those files *are* the production credential path and are
# supposed to reference it; this test is about it never leaking into the
# dev/CI-facing surface, not about it not existing at all.
_DEV_AND_CI_FILES = (
    REPO_ROOT / "deploy" / "compose.dev.yaml",
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
)


def test_production_secret_paths_are_not_referenced_in_dev_or_ci_config() -> None:
    for path in _DEV_AND_CI_FILES:
        assert path.is_file(), f"expected dev/CI config file missing: {path}"
        content = path.read_text()
        for marker in _PRODUCTION_SECRET_MARKERS:
            assert marker not in content, (
                f"{path} references {marker!r} -- a dev/CI-facing file must never route at "
                "production's systemd-decrypted credential material (docs/security-model.md "
                "invariant 4)"
            )


def test_dev_compose_uses_only_synthetic_placeholder_credentials() -> None:
    """`deploy/compose.dev.yaml` is the file every local `finance`/pytest
    run and CI job point at. It must carry an obviously-synthetic
    password, not something that could be mistaken for (or copy-pasted
    from) a real credential."""
    content = (REPO_ROOT / "deploy" / "compose.dev.yaml").read_text()
    assert "devpassword" in content, (
        "deploy/compose.dev.yaml's bootstrap password changed; re-verify it's still an "
        "obviously-synthetic placeholder, not something that looks like a real credential"
    )
