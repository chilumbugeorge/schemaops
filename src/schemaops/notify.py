"""Chat notifications. Set SCHEMAOPS_NOTIFY_WEBHOOK to a Slack-compatible incoming webhook URL."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from schemaops import log

logger = log.get("notify")


def send(text: str) -> bool:
    """Post a message. Never raises: a failed notification must not fail a deployment."""
    url = os.environ.get("SCHEMAOPS_NOTIFY_WEBHOOK", "")
    if not url:
        return False
    log.register_secret(url)  # a webhook URL is a credential
    pipeline = os.environ.get("CI_PIPELINE_URL", "")
    body = json.dumps({"text": log.redact(text + (f"\n{pipeline}" if pipeline else ""))}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - operator-configured webhook
            return True
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("notification failed", extra={"reason": log.redact(str(exc))})
        return False
