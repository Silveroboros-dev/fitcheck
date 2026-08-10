"""Compliance vocabulary gate (axiom A7).

One deterministic check, used in two places: eval runs and the response
boundary of every surface (HTTP and MCP). An agent client repeating our
output as trading advice is our copy problem too — so the filter runs on
responses, not just marketing copy.

This list is governed: additions/removals go through Loop 4, because
copy discipline is a shipped behavior, not a style preference.
"""

import re

# Phrases forbidden outright, matched case-insensitively.
RESTRICTED_PHRASES: tuple[str, ...] = (
    "guaranteed edge",
    "risk-free",
    "risk free",
    "perfect hedge",
    "you should trade",
    "you should buy",
    "you should sell",
    "sure thing",
)

# Standalone trading verbs are forbidden as imperatives/recommendations;
# matched as whole words to avoid false hits inside other words.
_RESTRICTED_WORDS: tuple[str, ...] = ("buy", "sell")

_PHRASE_RE = re.compile(
    "|".join(re.escape(p) for p in RESTRICTED_PHRASES), re.IGNORECASE
)
_WORD_RE = re.compile(
    r"\b(" + "|".join(_RESTRICTED_WORDS) + r")\b", re.IGNORECASE
)


def vocabulary_violations(text: str) -> list[str]:
    """Return the distinct restricted terms found in `text` (empty = clean)."""
    found = {m.group(0).lower() for m in _PHRASE_RE.finditer(text)}
    found |= {m.group(0).lower() for m in _WORD_RE.finditer(text)}
    return sorted(found)


def is_compliant(text: str) -> bool:
    return not vocabulary_violations(text)
