"""What this runtime raises, and what it deliberately does not.

Two errors, both about a call that cannot be dispatched: an unregistered
kind and a method that is not there. Everything else a call can do - fail,
time out, raise something of the application's own - travels to the caller
untouched, because a runtime that wrapped those would be deciding what an
application's failures mean.
"""

from __future__ import annotations


class GrainError(Exception):
    """Base class, so a caller can catch this runtime and nothing else."""


class GrainNotRegisteredError(GrainError, LookupError):
    """No behaviour is registered for the kind an identity names.

    A `LookupError` as well, because that is what it is: the address was
    well-formed and nothing answers at it.

    Attributes:
        grain_type: The kind that was asked for.
    """

    def __init__(self, grain_type: str) -> None:
        """Names the kind.

        Args:
            grain_type: The unregistered kind.
        """
        super().__init__(f"no grain type registered as {grain_type!r}")
        self.grain_type = grain_type


class NoSuchGrainMethodError(GrainError, AttributeError):
    """The grain has no callable public method by that name.

    An `AttributeError` as well, for the same reason: the call named
    something the object does not offer. Private names are refused here
    rather than merely absent, so a caller cannot reach an implementation
    detail by knowing its name - a call arrives from outside, and one day
    from off this node.

    Attributes:
        grain: The identity that was called.
        method: The name that did not resolve.
    """

    def __init__(self, grain: str, method: str) -> None:
        """Names both halves.

        Args:
            grain: Identity of the grain called.
            method: The method name.
        """
        super().__init__(f"{grain} has no callable method {method!r}")
        self.grain = grain
        self.method = method
