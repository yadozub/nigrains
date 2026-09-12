"""Calling a grain by name, checked as if you were calling the object.

``Runtime.call`` takes a method name and arguments because a call that will
one day cross a node boundary cannot carry a closure. That is the right
shape for a transport and the wrong shape for a caller: a renamed method
becomes a runtime error at the one moment nobody is watching.

A reference closes that without changing the transport. It is typed as the
grain class, so a type checker validates every call site against the grain's
own signatures, and the call underneath is still a name and a tuple.

    ref = runtime.reference(MemoryGrain, str(memory_id))
    hits = await ref.similar(query, k=20)      # checked against MemoryGrain

**It is not an instance of the grain, and the type checker is being told a
useful lie.** ``isinstance(ref, MemoryGrain)`` is False; the grain being
referred to may not be activated, may be on another machine, and may be
activated twice. What the annotation buys is the signatures, which is what
was missing; what it cannot buy is identity, which was never on offer -
holding the object is precisely what this model exists to stop you doing.

Two things keep the lie from becoming a lie about behaviour. Only
coroutine methods are reachable, so reading state through a reference fails
rather than returning something that looks like it worked. And the name is
resolved against the class when the attribute is touched, not when the call
lands, so a method that does not exist is an error where it is written.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from nigrains.errors import NoSuchGrainMethodError
from nigrains.grain import Grain, GrainId

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from nigrains.runtime import Runtime


class _Reference:
    """Forwards attribute access to a grain as a call through the runtime.

    Attributes:
        _runtime: Where the call is dispatched.
        _id: Which grain it goes to.
        _grain: The class the names are resolved against.
    """

    # Deliberately not slotted, which costs about a hundred bytes and buys
    # back a microsecond per call. __getattr__ runs only for attributes that
    # are *missing*, so caching the caller on the instance the first time a
    # method is named turns every later use into a plain lookup - and a
    # reference is usually held and called many times. Measured: resolving
    # per call put dispatch at 2.30 us against 1.23 for Runtime.call.
    #
    # The three below are annotated as well as assigned, because this class
    # defines __getattr__ and a type checker would otherwise resolve them
    # through it and conclude that `self._runtime` is a coroutine function.
    _runtime: Runtime
    _id: GrainId
    _grain: type[Grain]

    def __init__(self, runtime: Runtime, grain_id: GrainId, grain: type[Grain]) -> None:
        """Binds a reference to one identity.

        Args:
            runtime: Where to dispatch.
            grain_id: Which grain.
            grain: The class whose methods may be called.
        """
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_id", grain_id)
        object.__setattr__(self, "_grain", grain)

    def __getattr__(self, name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        """Resolves a method name against the class and returns a caller.

        Args:
            name: The method being asked for.

        Returns:
            A coroutine function forwarding to the runtime.

        Raises:
            NoSuchGrainMethodError: The class has no public coroutine method
                by that name. Raised here rather than at the call, so the
                failure lands on the line that names it.
        """
        attribute = None if name.startswith("_") else getattr(self._grain, name, None)
        if attribute is None or not inspect.iscoroutinefunction(attribute):
            raise NoSuchGrainMethodError(str(self._id), name)

        runtime, grain_id = self._runtime, self._id

        async def call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - the grain's own
            return await runtime.call(grain_id, name, *args, **kwargs)

        object.__setattr__(self, name, call)
        return call

    def __repr__(self) -> str:
        """Names what this refers to rather than what it is.

        Returns:
            A description carrying the identity.
        """
        return f"<reference to {self._id}>"


def reference_to[G: Grain](runtime: Runtime, grain: type[G], key: str) -> G:
    """Returns a reference to one grain, typed as that grain.

    Args:
        runtime: Where calls are dispatched.
        grain: The grain class, which supplies both the type name and the
            signatures a caller is checked against.
        key: Which grain of that kind.

    Returns:
        Something that answers the grain's coroutine methods. Typed as the
        grain; see this module's docstring for what that does and does not
        mean.

    Raises:
        ValueError: The class declares no ``grain_type``. A grain without
            one has no address, and the failure belongs here rather than at
            the first call.
    """
    grain_type = getattr(grain, "grain_type", "")
    if not grain_type:
        raise ValueError(f"{grain.__name__} declares no grain_type, so it has no address")
    return cast("G", _Reference(runtime, GrainId(grain_type, key), grain))
