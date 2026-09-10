"""Unit tests for log sanitization (handoff §23, docs/security-model.md
invariant 6 — never log secrets or financial payloads)."""

from finance_app.ops.logging import sanitize_context


def test_redacts_keys_that_look_like_secrets() -> None:
    context = {
        "plaid_access_token": "access-sandbox-xyz",
        "openai_api_key": "sk-abc",
        "database_password": "hunter2",
        "AUTHORIZATION": "Bearer abc",
        "webhook_secret": "shh",
        "backup_passphrase": "shh-too",
        "credential_id": "cred-1",
    }

    result = sanitize_context(context)

    for key in context:
        assert result[key] == "[redacted]", key


def test_leaves_ordinary_operational_fields_untouched() -> None:
    context = {"run_id": "abc-123", "added_count": 5, "status": "success"}

    assert sanitize_context(context) == context


def test_redacts_recursively_in_nested_dicts() -> None:
    context = {"outer": {"plaid_secret": "shh", "count": 3}}

    result = sanitize_context(context)

    assert result["outer"]["plaid_secret"] == "[redacted]"
    assert result["outer"]["count"] == 3


def test_empty_or_none_context_is_safe() -> None:
    assert sanitize_context(None) == {}
    assert sanitize_context({}) == {}
