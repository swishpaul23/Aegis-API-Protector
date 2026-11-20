"""Tell a human a fix needs review. Notices carry field paths and counts, never values."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class PendingNotice(BaseModel):
    model_key: str
    pending_id: int | None = None
    fields: list[str]
    min_confidence: float | None = None
    quarantined: int = 0
    message: str


def _check_url(url: str) -> None:
    """Raise ValueError (never echoing the URL: it may hold a secret token)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise ValueError("webhook URL is not a valid URL") from None
    if not host:
        raise ValueError("webhook URL is not a valid URL")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and host in LOCAL_HOSTS:
        return
    raise ValueError("webhook URL must use https (http only for localhost)")


def _text(n: PendingNotice) -> str:
    pending = f"pending #{n.pending_id}" if n.pending_id is not None else "pending"
    return (
        f"Aegis: {len(n.fields)} field fix(es) need review for {n.model_key} "
        f"({pending}, {n.quarantined} record(s) quarantined). Run: aegis review"
    )


def webhook_notifier(
    url: str,
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> Callable[[PendingNotice], None]:
    """POST `{"text", "aegis"}` JSON to a webhook (Slack/Discord-compatible `text`).

    Failures are logged by status code or exception type only, then swallowed.
    """
    _check_url(url)
    log = logging.getLogger("aegis_healer")

    def send(notice: PendingNotice) -> None:
        body = {"text": _text(notice), "aegis": notice.model_dump(mode="json")}
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,  # no proxy/.netrc influence on the webhook request
                transport=transport,
            ) as client:
                response = client.post(url, json=body)
        except httpx.HTTPError as exc:
            log.warning("webhook notification failed: %s", type(exc).__name__)
            return
        if not response.is_success:
            log.warning("webhook notification failed: HTTP %d", response.status_code)

    return send
