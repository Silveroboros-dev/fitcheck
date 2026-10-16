"""Immutable policy data for current services, preserving frozen v1 replay.

``policy.py`` remains the byte-pinned historical verifier. This representation
does not change its constants, policy identity, or serialized horizon values.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from el.fitgate.policy import FitPolicy as LegacyFitPolicy


class _ImmutableDict(dict):
    """JSON- and pickle-compatible mapping that rejects in-place mutation."""

    _error = "fit-policy data are immutable"

    def __init__(self, values=()):
        if getattr(self, "_initialized", False):
            self._immutable()
        items = values.items() if hasattr(values, "items") else values
        dict.__init__(self, items)
        self._initialized = True

    def _immutable(self, *args, **kwargs):
        raise TypeError(self._error)

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return type(self), (dict(self),)


class _ImmutableList(list):
    """JSON- and pickle-compatible sequence that rejects in-place mutation."""

    def __init__(self, values=()):
        if getattr(self, "_initialized", False):
            self._immutable()
        list.__init__(self, values)
        self._initialized = True

    def _immutable(self, *args, **kwargs):
        raise TypeError("fit-policy data are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return type(self), (list(self),)


class _HorizonTolerances(_ImmutableDict):
    """Read-only dict with JSON/dataclass serialization compatible with v1."""

    _error = "fit-policy horizon tolerances are immutable"

    def __init__(self, values):
        if getattr(self, "_initialized", False):
            self._immutable()
        items = values.items() if hasattr(values, "items") else values
        super().__init__((precision, tuple(days)) for precision, days in items)


def _deep_freeze(value):
    """Detach and freeze policy trees without changing their wire shape."""
    if isinstance(value, (_ImmutableDict, _ImmutableList)):
        return value
    if isinstance(value, BaseModel):
        if not type(value).model_config.get("frozen"):
            raise TypeError(
                "fit-policy data contain a mutable Pydantic model: "
                f"{type(value).__name__}"
            )
        return value.model_copy(
            update={
                name: _deep_freeze(getattr(value, name))
                for name in type(value).model_fields
            }
        )
    if isinstance(value, Mapping):
        return _ImmutableDict(
            (_deep_freeze(key), _deep_freeze(item))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return _ImmutableList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    if value is None or isinstance(
        value,
        (
            str,
            bytes,
            int,
            float,
            bool,
            Enum,
            date,
            datetime,
            time,
            timedelta,
            Decimal,
            UUID,
        ),
    ):
        return value
    raise TypeError(
        "fit-policy data contain an unsupported mutable value: "
        f"{type(value).__name__}"
    )


def _is_deeply_immutable(value) -> bool:
    if isinstance(value, _ImmutableDict):
        return all(
            _is_deeply_immutable(key) and _is_deeply_immutable(item)
            for key, item in value.items()
        )
    if isinstance(value, _ImmutableList):
        return all(_is_deeply_immutable(item) for item in value)
    if isinstance(value, BaseModel):
        return bool(type(value).model_config.get("frozen")) and all(
            _is_deeply_immutable(getattr(value, name))
            for name in type(value).model_fields
        )
    if isinstance(value, (tuple, frozenset)):
        return all(_is_deeply_immutable(item) for item in value)
    return value is None or isinstance(
        value,
        (
            str,
            bytes,
            int,
            float,
            bool,
            Enum,
            date,
            datetime,
            time,
            timedelta,
            Decimal,
            UUID,
        ),
    )


@dataclass(frozen=True)
class FitPolicy(LegacyFitPolicy):
    """Same predecessor policy identity with defensively copied immutable data."""

    def __post_init__(self):
        object.__setattr__(
            self, "horizon_tolerances", _HorizonTolerances(self.horizon_tolerances)
        )
        object.__setattr__(
            self, "composite_coverage", _deep_freeze(self.composite_coverage)
        )


def immutable_fit_policy(policy: LegacyFitPolicy | None = None) -> LegacyFitPolicy:
    """Keep explicit policy type/identity while isolating mutable caller input."""
    if policy is None:
        return FitPolicy()
    if isinstance(
        policy.horizon_tolerances, _HorizonTolerances
    ) and _is_deeply_immutable(policy.composite_coverage):
        return policy
    return replace(
        policy,
        horizon_tolerances=_HorizonTolerances(policy.horizon_tolerances),
        composite_coverage=_deep_freeze(policy.composite_coverage),
    )
