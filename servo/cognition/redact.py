"""Strip secrets from requests before they reach memory, traces or the LLM provider.

A user pasted "email: … password: …" into the copilot; it ended up in plain text in
~/.servo/traces, chat memory and a cloud prompt. Servo never types passwords anyway
(see knowledge/recipes/sign_in_and_secrets.md), so the value is replaced with
"[redacted]" at the front door and nothing downstream ever sees it.
"""

from __future__ import annotations

import re
from typing import List, Tuple

REDACTED = "[redacted]"

_LABELLED = re.compile(
    r"(?i)\b(password|passwd|pwd|pass\s*word|passcode|pin(?:\s*code)?|otp|one[\s-]*time\s*code|"
    r"2fa(?:\s*code)?|verification\s*code|security\s*code|cvv|cvc|secret|api[\s_-]*key|token)"
    r"(\s*(?::|=)\s*|\s+is\s+)"
    r"(\"[^\"]+\"|'[^']+'|\S+)"
)
_KEY_SHAPES = [
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), "api key"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "api key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "access key"),
]
_CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")
_NOT_VALUES = {"is", "my", "the", "a", "and", "with", "for", "field", "box", "reset", "manager", "here", REDACTED}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact_secrets(text: str) -> Tuple[str, List[str]]:
    """(text with secrets replaced, kinds found)."""
    found: List[str] = []

    def labelled(m: "re.Match[str]") -> str:
        value = m.group(3).strip("\"'")
        if value.lower().rstrip(".,") in _NOT_VALUES or len(value) < 3:
            return m.group(0)
        found.append(m.group(1).lower())
        return f"{m.group(1)}{m.group(2)}{REDACTED}"

    out = _LABELLED.sub(labelled, text or "")
    for rx, kind in _KEY_SHAPES:
        out, n = rx.subn(REDACTED, out)
        found += [kind] * n

    def card(m: "re.Match[str]") -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card number")
            return REDACTED
        return m.group(0)

    out = _CARD.sub(card, out)
    return out, found
