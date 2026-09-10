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

_REDACTED = "[redacted]"


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
        elif isinstance(value, dict):
            clean[key] = sanitize_context(value)
        else:
            clean[key] = value
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
