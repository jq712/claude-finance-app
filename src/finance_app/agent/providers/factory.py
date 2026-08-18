"""Selects the configured `AgentProvider` adapter (ADR-014). The only
place `Settings.agent_provider` is read to decide which SDK client gets
constructed — everything above this is provider-agnostic."""

from finance_app.agent.providers.anthropic import AnthropicProvider
from finance_app.agent.providers.base import AgentProvider
from finance_app.agent.providers.openai import OpenAIProvider
from finance_app.config.settings import Settings


def build_provider(settings: Settings) -> AgentProvider:
    """Construct the active provider's adapter. Raises
    `MissingProviderCredentialError` up front (via
    `Settings.active_agent_credential`) rather than deep inside a
    conversation turn — see handoff §8.4."""
    credential = settings.active_agent_credential().get_secret_value()
    if settings.agent_provider == "openai":
        return OpenAIProvider(api_key=credential, model=settings.openai_model or "gpt-4o")
    return AnthropicProvider(
        api_key=credential, model=settings.anthropic_model or "claude-sonnet-4-5"
    )
