"""What a call looks like when it has to leave the process.

Two things a transport needs and neither of them is transport: a way to turn
a call into bytes, and a way to carry a failure back that does not lose what
it was.

**Serialization is the caller's choice and JSON is only the default.** A
grain taking a dataclass and returning a `Decimal` will not survive JSON,
and the honest answer to that is a codec of your own rather than a guess
about what you meant. The port is two methods.

**A failure crossing a wire cannot arrive as the object that left.** JSON
carries a name and a message, not a traceback and not a class. So what comes
back is one of two things, and the difference is stated rather than blurred:
an exception this package defines is rebuilt, because both ends know it; and
everything else arrives as :class:`RemoteError`, carrying the type's name
and the message, so a caller can see what happened and cannot catch it by
its original class. A transport that pretended otherwise would be lying
about identity, and a transport that turned everything into one error would
make every remote grain untestable.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from nigrains.call import DeadlineExceeded
from nigrains.errors import GrainError, GrainNotRegisteredError, NoSuchGrainMethodError
from nigrains.state import ConcurrentChange


class RemoteError(GrainError):
    """A grain on another node raised something this one cannot rebuild.

    Its name and message survived; its class and traceback did not. Catching
    this is catching "something went wrong over there", and the attributes
    are how to find out what.

    Attributes:
        type_name: What the far side called it.
        message: What it said.
    """

    def __init__(self, type_name: str, message: str) -> None:
        """Carries across what a wire can carry.

        Args:
            type_name: The exception class name on the far side.
            message: Its message.
        """
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name
        self.message = message


_REBUILDABLE: dict[str, type[BaseException]] = {
    "DeadlineExceeded": DeadlineExceeded,
    "ConcurrentChange": ConcurrentChange,
    "NoSuchGrainMethodError": NoSuchGrainMethodError,
    "GrainNotRegisteredError": GrainNotRegisteredError,
}
"""Exceptions both ends are guaranteed to know, so they cross as themselves.

Only this package's own, and only the ones a caller has a reason to catch by
class: a deadline that ran out, a write that lost a race, a call to a method
or a kind that is not there. Everything else is somebody's own exception,
and guessing how to rebuild it from a name and a message is how a library
invents a constructor that does not exist.
"""


@runtime_checkable
class Codec(Protocol):
    """Turns what crosses the wire into bytes and back."""

    def encode(self, value: Any) -> bytes:  # noqa: ANN401 - the caller's own
        """Renders a value.

        Args:
            value: What to send.

        Returns:
            The bytes.

        Raises:
            Exception: The value cannot be rendered. Better here, on the
                sending side, than as something unreadable on the far one.
        """
        ...

    def decode(self, raw: bytes) -> Any:  # noqa: ANN401 - the caller's own
        """Reads a value back.

        Args:
            raw: What arrived.

        Returns:
            The value.
        """
        ...


class JsonCodec:
    """The default: JSON, and nothing clever.

    Enough for a grain whose arguments and answers are numbers, strings,
    lists and dictionaries, which is most of them and not all of them. A
    grain that takes a dataclass wants a codec that knows about it, and this
    package would rather it said so than guess.
    """

    def encode(self, value: Any) -> bytes:  # noqa: ANN401 - the caller's own
        """Renders a value as JSON.

        Args:
            value: What to send.

        Returns:
            The bytes.

        Raises:
            TypeError: JSON cannot render it, which is the moment to reach
                for a codec that can.
        """
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def decode(self, raw: bytes) -> Any:  # noqa: ANN401 - the caller's own
        """Reads JSON back.

        Args:
            raw: What arrived.

        Returns:
            The value.
        """
        return json.loads(raw)


def failure_to_wire(exc: BaseException) -> dict[str, str]:
    """Renders an exception as the little a wire can carry.

    Args:
        exc: What the grain raised.

    Returns:
        Its type name and message.
    """
    return {"type": type(exc).__name__, "message": str(exc)}


def failure_from_wire(rendered: dict[str, str]) -> BaseException:
    """Rebuilds what crossed, or wraps what could not.

    Args:
        rendered: What the far side sent.

    Returns:
        The exception to raise. One of this package's own when both ends
        know it, and :class:`RemoteError` otherwise - never a guess at
        somebody else's constructor.
    """
    name = rendered.get("type", "Exception")
    message = rendered.get("message", "")
    known = _REBUILDABLE.get(name)
    if known is None:
        return RemoteError(name, message)
    rebuilt = known.__new__(known)
    BaseException.__init__(rebuilt, message)
    return rebuilt
