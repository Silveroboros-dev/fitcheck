"""Field-aware A7 response guard for the MCP boundary (session-6 amendment).

`vocabulary_violations` flags trading-advice phrases plus the whole words
"buy"/"sell". Run blindly over a whole response it would FALSE-FAIL on quoted
source — a market titled "Will X sell Y?", captured resolution rules, or a
user's pasted thesis. So this guard checks only SYSTEM-GENERATED copy and
EXEMPTS declared source fields: hard-fail our advice, never punish source.

Mechanism: walk the response payload building index-agnostic dotted paths
(lists collapse to `field[]`); every leaf string whose path is NOT in the
caller-declared `source_paths` is run through `vocabulary_violations`. Any hit
raises A7Violation (fail closed) — the tool returns an error, never the copy.
"""

from el.domain.vocabulary import vocabulary_violations


class A7Violation(Exception):
    """Restricted trading/advice vocabulary in a system-generated field."""

    def __init__(self, offenders: list[tuple[str, list[str]]]):
        self.offenders = offenders
        detail = "; ".join(f"{path}: {terms}" for path, terms in offenders)
        super().__init__(f"A7 violation in system-generated field(s) — {detail}")


def _walk(
    node: object,
    path: str,
    source_paths: frozenset[str],
    out: list[tuple[str, list[str]]],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            _walk(value, child, source_paths, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, f"{path}[]", source_paths, out)
    elif isinstance(node, str):
        if path in source_paths:
            return  # declared quoted/source field — exempt
        terms = vocabulary_violations(node)
        if terms:
            out.append((path, terms))


def assert_a7_clean(payload: dict, *, source_paths: frozenset[str] = frozenset()) -> None:
    """Raise A7Violation if any non-source string carries restricted vocab.

    `source_paths` are index-agnostic dotted paths to EXEMPT (quoted user text,
    market titles, captured rules, etc.) — everything else is treated as
    system-generated copy and checked (fail closed)."""
    offenders: list[tuple[str, list[str]]] = []
    _walk(payload, "", source_paths, offenders)
    if offenders:
        raise A7Violation(offenders)
