"""Structured, sanitized logging for `finance`/`finops` (handoff §23,
CLAUDE.md "Financial correctness"/"NEVER" lists, docs/security-model.md
invariant 6).

Two hard rules this module exists to make easy to follow correctly:

1. Logs are structured (JSON to stdout, which systemd/journald captures
   for every unit — see deploy/systemd/) so `journalctl -u finance-sync`
   output is greppable and machine-readable, not prose.
2. Logs never carry secrets or financial payloads. `sanitize_context`
   is the single place that redaction happens, so every call site gets it
   "for free" instead of remembering it ad hoc at every log statement.
"""

import datetime
import json
import logging
import re
import sys
from typing import Any

# Keys that must never appear in a log record's structured context, even if
# a caller passes them by accident. Matched case-insensitively; substring
# match on purpose (e.g. "plaid_access_token" and "openai_api_key" both
# get caught by "token"/"key"/"secret"/"password").
_REDACTED_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "passphrase",
    "credential",
)

# Key-name matching alone misses a secret that lands in a context dict
# under an innocuous key -- e.g. `{"detail": f"connection failed: {dsn}"}`
# where `dsn` embeds a role password, or a raw provider API key pasted
# into an error message. These patterns catch the *shape* of a secret
# regardless of what key it's filed under. Deliberately conservative in
# the same direction as the key-fragment list: a false positive here
# costs nothing (this is diagnostic logging, not user-facing output); a
# real secret reaching a log line is what CLAUDE.md's "NEVER" list exists
# to prevent.
_VALUE_PATTERNS = (
    # scheme://user:password@host -- any DSN-shaped string with an
    # embedded credential (postgresql[+psycopg]://, etc).
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@"),
    # OpenAI/Anthropic-style API keys (both use an `sk-` prefix).
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),
)

_REDACTED = "[redacted]"


def _looks_like_a_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _VALUE_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    """Recurse into dicts and lists (QA/security-model.md invariant 6 —
    `sanitize_context` previously only recursed into dicts, so a secret
    inside a list value was never redacted at all) and value-scan strings
    for secret-shaped content regardless of the key they're filed under."""
    if isinstance(value, dict):
        return sanitize_context(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str) and _looks_like_a_secret(value):
        return _REDACTED
    return value


def sanitize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Strip anything that looks like a secret from a logging context dict.

    Deliberately conservative: a false positive (redacting a harmless key
    that happens to contain "key") is cheap. A false negative (a real
    secret reaching a log line) is not. See CLAUDE.md "NEVER" list.
    """
    if not context:
        return {}
    clean: dict[str, Any] = {}
    for key, value in context.items():
        lowered = key.lower()
        if any(fragment in lowered for fragment in _REDACTED_KEY_FRAGMENTS):
            clean[key] = _REDACTED
        else:
            clean[key] = _sanitize_value(value)
    return clean


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            payload["run_id"] = run_id
        context = getattr(record, "context", None)
        if context:
            payload["context"] = sanitize_context(context)
        if record.exc_info:
            # Sanitized exception *class*, per CLAUDE.md — never the raw
            # exception message, which may embed request/response detail.
            exc_type = record.exc_info[0]
            payload["exception_class"] = exc_type.__name__ if exc_type else "Unknown"
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger once. Safe to call multiple times —
    subsequent calls are no-ops so CLI commands can call it unconditionally
    on startup."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    _configured = True


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    run_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Emit one structured operational event. Prefer this over
    `logger.info(f"...")` string interpolation for anything carrying
    identifiers/counts, so the run_id/context land as structured fields."""
    logger.log(level, message, extra={"run_id": run_id, "context": sanitize_context(context)})
