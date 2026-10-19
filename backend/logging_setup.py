"""Logging configuration that keeps credentials out of log records.

Phase 3.5. Phase 3 introduced short-lived media tokens passed in the
`access_token` query parameter, because a browser `<video>` element cannot send
an `Authorization` header. That makes the request target itself sensitive: any
log line containing the full path would record a usable credential.

This module installs a filter on the loggers that emit request targets
(`uvicorn.access` in particular) so those values are replaced before a record
is formatted.

WHAT THIS DOES NOT COVER. Only logging performed inside this process is
affected. A reverse proxy, load balancer, CDN, API gateway, or hosting
platform writes its own access logs from the raw request line, and a browser
records the URL in history and may send it in a `Referer` header. Those are
deployment concerns and are documented in `docs/SECURITY_OPERATIONS.md`; do
not assume this filter addresses them.
"""

from __future__ import annotations

import logging

from rate_limit import redact_query_string

# Loggers known to emit a full request target or URL. `uvicorn.access` is the
# server access log. The httpx/httpcore loggers emit outgoing request URLs and
# are included because anything in this process that fetches a tokenised URL
# (including the test client) would otherwise log the credential. Any new
# logger that prints a URL must be added here or passed to
# install_log_redaction(extra_loggers=...).
_REQUEST_LOGGERS = (
    "uvicorn.access",
    "uvicorn.error",
    "httpx",
    "httpx2",
    "httpcore",
)

# Substrings that mark a sensitive query parameter. Checked before the more
# expensive rewrite so ordinary log lines are untouched.
_SENSITIVE_MARKERS = ("access_token=", "token=", "signature=", "sig=", "key=")


class RedactQueryStringFilter(logging.Filter):
    """Rewrite sensitive query-parameter values in log records.

    Handles both shapes a request target can arrive in:

    * as a positional formatting argument, which is how `uvicorn.access`
      emits it (``'%s - "%s %s HTTP/%s" %d'`` with the target as an argument);
    * embedded in an already-formatted message.

    A filter is used rather than a custom formatter so it applies regardless of
    how the handler is configured, including under a deployment's own logging
    setup.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = self._redact_args(record.args)

        message = record.msg
        if isinstance(message, str) and self._is_sensitive(message):
            record.msg = redact_query_string(message)

        # Always keep the record; this filter only sanitises it.
        return True

    @staticmethod
    def _is_sensitive(value: str) -> bool:
        return "?" in value and any(m in value for m in _SENSITIVE_MARKERS)

    @classmethod
    def _redact_args(cls, args):
        def scrub(value):
            # Not all URL arguments are plain strings: httpx logs an
            # `httpx.URL` object, and other libraries use their own URL types.
            # Inspect the string form so those are covered too, and return a
            # redacted string in their place.
            if isinstance(value, (str, bytes)) or hasattr(value, "__str__"):
                try:
                    text = value.decode() if isinstance(value, bytes) else str(value)
                except Exception:  # pragma: no cover - defensive
                    return value
                if cls._is_sensitive(text):
                    return redact_query_string(text)
            return value

        if isinstance(args, dict):
            return {key: scrub(value) for key, value in args.items()}
        if isinstance(args, tuple):
            return tuple(scrub(value) for value in args)
        return args


def install_log_redaction(extra_loggers: tuple[str, ...] = ()) -> None:
    """Attach the redaction filter to the request-emitting loggers.

    Idempotent: calling it twice does not add a second filter, so it is safe to
    invoke from both application startup and a test fixture.
    """
    for name in _REQUEST_LOGGERS + tuple(extra_loggers):
        logger = logging.getLogger(name)
        if any(isinstance(f, RedactQueryStringFilter) for f in logger.filters):
            continue
        logger.addFilter(RedactQueryStringFilter())


__all__ = ("RedactQueryStringFilter", "install_log_redaction")
