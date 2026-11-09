"""Stub email provider — writes each message to disk for offline preview.

Useful in tests and during local dev when the partner doesn't want to wire
real SMTP credentials yet. Each ``send()`` writes two sibling files under
``EMAIL_STUB_DIR``:

  - ``YYYYMMDDTHHMMSS_<tag>_<to>.json`` — metadata (from, to, subject, tag)
  - ``YYYYMMDDTHHMMSS_<tag>_<to>.html`` — rendered body (HTML if present
                                          else text)

The partner can open the .html files in a browser to preview templates.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.services.email.base import EmailMessage, EmailProvider


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:80]


class StubEmailProvider(EmailProvider):
    name = "stub"

    def __init__(self, *, output_dir: str) -> None:
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    async def send(self, message: EmailMessage) -> str:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = _safe(message.tag or "msg")
        to_part = _safe(message.to[0] if message.to else "unknown")
        message_id = uuid4().hex
        base = self._dir / f"{ts}_{suffix}_{to_part}_{message_id[:8]}"

        metadata = {
            "message_id": message_id,
            "from": f"{message.from_name} <{message.from_address}>",
            "to": list(message.to),
            "reply_to": message.reply_to,
            "subject": message.subject,
            "tag": message.tag,
            "timestamp": ts,
        }
        base.with_suffix(".json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        body = message.body_html if message.body_html else f"<pre>{message.body_text}</pre>"
        base.with_suffix(".html").write_text(body, encoding="utf-8")
        return message_id
