"""Private, process-local bindings for one retained iOS startup.

The binding is deliberately inert outside the Lab that issued it.  Its JSON
payloads are kept as canonical private strings so callers cannot mutate the
payload after issuance through a returned dictionary.
"""
from __future__ import annotations

import json


def _document(value):
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        parsed = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Retained startup payload is not JSON data") from None
    if parsed != value:
        raise ValueError("Retained startup payload is not canonical JSON data")
    return encoded


class RetainedStartupBinding:
    """Opaque one-use binding issued by exactly one :class:`Lab` instance."""

    __slots__ = (
        "_issuer",
        "_scope",
        "_owner",
        "_provider",
        "_logical_document",
        "_native_document",
        "_provider_incarnation",
        "_sealed",
    )

    def __init__(self, *, issuer, scope, owner, provider, logical_payload,
                 native_payload, provider_incarnation):
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_logical_document", _document(logical_payload))
        object.__setattr__(self, "_native_document", _document(native_payload))
        object.__setattr__(self, "_provider_incarnation", provider_incarnation)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("Retained startup binding is immutable")
        object.__setattr__(self, name, value)

    @property
    def scope(self):
        return self._scope

    @property
    def owner(self):
        return self._owner

    @property
    def provider(self):
        return self._provider

    @property
    def logical_payload(self):
        return json.loads(self._logical_document)

    @property
    def native_payload(self):
        return json.loads(self._native_document)

    @property
    def provider_incarnation(self):
        return self._provider_incarnation

    def __repr__(self):
        return "<RetainedStartupBinding>"

    def __copy__(self):
        return self._unregistered_copy()

    def __deepcopy__(self, memo):
        copied = self._unregistered_copy()
        memo[id(self)] = copied
        return copied

    def _unregistered_copy(self):
        copied = object.__new__(type(self))
        for name in self.__slots__:
            object.__setattr__(copied, name, getattr(self, name))
        return copied


__all__ = ["RetainedStartupBinding"]
