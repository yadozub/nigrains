"""What one call is, and the two things that wrap around it.

A call through a runtime is a small amount of data - which grain, which
method, what arguments - and two cross-cutting concerns that every user of
a runtime like this eventually wants and otherwise gets by patching it:
somewhere to hang logging, tracing, retry and authorisation, and a deadline
that survives being passed along.

Both are here rather than in the runtime because a transport will one day
have to carry them, and a shape decided after the wire exists is a shape
decided twice.

**The deadline travels in a context variable, not in an argument.** A grain
calling another grain should inherit what is left of the caller's patience
without either of them naming it, and an argument would have to be threaded
through every signature in between - including grain methods, whose
signatures belong to their authors. A context variable is exactly the tool
for a value that follows control flow, and asyncio propagates it into tasks.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nigrains.errors import GrainError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from nigrains.grain import GrainId

_deadline: ContextVar[float | None] = ContextVar("nigrains_deadline", default=None)
"""When the work in this context must be finished, on a monotonic clock.

None means nobody set one, which is not the same as "no time left"."""


class DeadlineExceeded(GrainError, TimeoutError):  # noqa: N818 - named after TimeoutError,
    # which callers already catch and which does not carry the suffix either
    """The time allowed for a call ran out.

    A ``TimeoutError`` as well, because that is what it is and because
    callers already catch that. Raised before dispatch when there was no
    time left to begin with, and from the call itself when it ran out
    partway - the two are worth telling apart in a log and not worth telling
    apart in a handler.
    """

    def __init__(self, grain: str, method: str) -> None:
        """Names what did not finish.

        Args:
            grain: Identity of the grain called.
            method: The method that did not finish in time.
        """
        super().__init__(f"deadline exceeded calling {method!r} on {grain}")
        self.grain = grain
        self.method = method


@dataclass(slots=True)
class Call:
    """One invocation, as data, on its way to a grain.

    What a filter sees and what a transport will put on a wire. The
    arguments are the caller's own objects here and will be whatever
    survives serialization there, which is a difference a filter should not
    pretend away.

    Attributes:
        grain: Which grain.
        method: Which of its methods.
        args: Positional arguments.
        kwargs: Keyword arguments.
        deadline: When this must be finished, on the runtime's clock, or
            None when nobody set one.
    """

    grain: GrainId
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    deadline: float | None = None


type Next = Callable[[Call], Awaitable[Any]]
"""The rest of the chain, as a filter receives it."""

type CallFilter = Callable[[Call, Next], Awaitable[Any]]
"""A filter wraps a call and the rest of the chain.

    async def timing(call: Call, nxt: Next) -> Any:
        started = time.perf_counter()
        try:
            return await nxt(call)
        finally:
            log.info("%s.%s took %.3fs", call.grain, call.method,
                     time.perf_counter() - started)

Filters run in the order they are given, outermost first, and the innermost
thing they wrap is the dispatch itself. A filter that does not await its
``next`` does not call the grain, which is how a cache or a refusal is
written and also how one is written by accident.

**A filter can return something the method never could, and nothing checks
that.** A typed reference promises a caller that ``ref.increment()`` returns
what ``increment`` returns; a filter sitting in the middle can return a
string. The type checker cannot see past the chain and will not warn. That
is the price of having filters at all, and the rule that follows is worth
stating: a filter that substitutes a result should substitute one of the
same type, and a filter that cannot should raise instead.
"""


@contextmanager
def deadline(seconds: float, *, clock: Callable[[], float] = time.monotonic) -> Iterator[None]:
    """Gives the calls made inside a limit on how long they may take.

    Nested use takes the **earlier** of the two: an inner block asking for
    longer than it has been given does not get it, because the outer caller
    has already promised somebody else.

        async with runtime:
            with deadline(2.0):
                await ref.slow_thing()

    Args:
        seconds: How long from now.
        clock: Monotonic source of the current time; the runtime's own, when
            a test is driving it.

    Yields:
        Nothing; the limit applies to what runs inside.
    """
    asked = clock() + seconds
    current = _deadline.get()
    token = _deadline.set(asked if current is None else min(asked, current))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining(*, clock: Callable[[], float] = time.monotonic) -> float | None:
    """How long is left, for a grain that wants to decide for itself.

    A grain doing something long can ask, and shorten its own work rather
    than being cut off in the middle of it - which is the difference between
    a deadline and a kill.

    Args:
        clock: Monotonic source of the current time.

    Returns:
        Seconds left, which may be negative; or None when no deadline is
        set. None is not "no time" and a caller must not treat it as zero.
    """
    at = _deadline.get()
    return None if at is None else at - clock()


def current_deadline() -> float | None:
    """The deadline in force, on the monotonic clock.

    Returns:
        The deadline, or None.
    """
    return _deadline.get()
