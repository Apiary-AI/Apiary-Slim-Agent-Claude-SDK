"""Strip known secret patterns from text before it leaves the container.

Applied to outbound text on two boundaries: Telegram streaming and Superpos
task result/error payloads. Defence-in-depth — the first line of defence
is not putting tokens in URLs / logs in the first place.

Patterns covered:
  - GitHub classic PAT (`ghp_…`, 36 chars)
  - GitHub fine-grained PAT (`github_pat_…`)
  - Anthropic API key (`sk-ant-api…`)
  - Anthropic OAuth token (`sk-ant-oat…`)

Redaction preserves a short prefix so debugging "which key leaked?" is still
possible without revealing the secret.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bghp_[A-Za-z0-9]{30,40}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,100}\b"),
    re.compile(r"\bsk-ant-api[0-9]{2}-[A-Za-z0-9_-]{80,120}\b"),
    re.compile(r"\bsk-ant-oat[0-9]*-[A-Za-z0-9_-]{20,}\b"),
]


def _mask(match: re.Match[str]) -> str:
    s = match.group(0)
    prefix_len = min(8, len(s) // 4)
    return f"{s[:prefix_len]}…[REDACTED]"


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(_mask, text)
    return text
