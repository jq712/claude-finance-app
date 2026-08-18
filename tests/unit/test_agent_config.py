"""Unit tests for the provider credential switch (ADR-014): only the
active provider's key is required, and a missing one fails with a clear
error rather than a runtime `KeyError` mid-conversation."""

import pytest
from pydantic import SecretStr

from finance_app.config.settings import MissingProviderCredentialError, Settings


@pytest.fixture(autouse=True)
def _no_ambient_provider_credentials(monkeypatch):
    """`Settings` reads real environment variables, and this machine's
    shell may export a genuine `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` for
    unrelated tools. Without this, a "missing credential" test would pass
    or fail depending on who's running it and what their shell exports —
    strip both so every test here is deterministic regardless of ambient
    environment."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "postgresql+psycopg://x:x@localhost/x",
        "alembic_database_url": "postgresql+psycopg://x:x@localhost/x",
        "agent_database_url": "postgresql+psycopg://x:x@localhost/x",
        "openai_api_key": SecretStr(""),
        "anthropic_api_key": SecretStr(""),
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_openai_provider_requires_only_openai_key() -> None:
    settings = _settings(agent_provider="openai", openai_api_key=SecretStr("sk-test"))
    assert settings.active_agent_credential().get_secret_value() == "sk-test"


def test_anthropic_provider_requires_only_anthropic_key() -> None:
    settings = _settings(agent_provider="anthropic", anthropic_api_key=SecretStr("sk-ant-test"))
    assert settings.active_agent_credential().get_secret_value() == "sk-ant-test"


def test_missing_openai_key_raises_clear_error() -> None:
    settings = _settings(agent_provider="openai")
    with pytest.raises(MissingProviderCredentialError, match="OPENAI_API_KEY"):
        settings.active_agent_credential()


def test_missing_anthropic_key_raises_clear_error() -> None:
    settings = _settings(agent_provider="anthropic")
    with pytest.raises(MissingProviderCredentialError, match="ANTHROPIC_API_KEY"):
        settings.active_agent_credential()


def test_inactive_provider_credential_may_be_unset() -> None:
    """The active provider is openai; anthropic's key being empty must not
    matter at all."""
    settings = _settings(
        agent_provider="openai",
        openai_api_key=SecretStr("sk-test"),
        anthropic_api_key=SecretStr(""),
    )
    settings.active_agent_credential()  # must not raise
