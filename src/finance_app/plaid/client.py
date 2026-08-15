"""Constructs the Plaid SDK client from `Settings`.

Never touches production credentials directly, never fetches secrets
outside `Settings` (`get_settings()` reads only from the environment/.env —
see CLAUDE.md and docs/security-model.md). Development uses
`PLAID_ENV=sandbox` exclusively; this module has no branch that treats a
missing/blank credential as "try anyway", so a misconfigured environment
fails at client construction rather than mid-sync.
"""

import plaid
from plaid.api.plaid_api import PlaidApi

from finance_app.config.settings import Settings, get_settings

_ENVIRONMENT_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def build_client(settings: Settings | None = None) -> PlaidApi:
    """A configured `PlaidApi` client. Does not make a network call."""
    settings = settings or get_settings()
    host = _ENVIRONMENT_HOSTS.get(settings.plaid_env)
    if host is None:
        raise ValueError(
            f"Unknown PLAID_ENV {settings.plaid_env!r}; expected one of "
            f"{sorted(_ENVIRONMENT_HOSTS)}"
        )
    if not settings.plaid_client_id or not settings.plaid_secret.get_secret_value():
        raise ValueError("PLAID_CLIENT_ID and PLAID_SECRET must be set to build a Plaid client")

    configuration = plaid.Configuration(
        host=host,
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret.get_secret_value(),
        },
    )
    api_client = plaid.ApiClient(configuration)
    return PlaidApi(api_client)
