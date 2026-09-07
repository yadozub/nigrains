"""The single-node half of the runtime: activation, dispatch, collection.

What this does is small and what it deliberately does not do is larger, so
both are written down.

**It activates on demand.** A call names a grain; if no activation exists,
one is built and :meth:`~nigrains.grain.Grain.activate` is awaited
before the call is served. Callers that arrive during that wait on the same
activation rather than starting a second - the thundering herd on a cold
grain is the normal case here, not the exceptional one, because the fleet is
cold exactly when a job starts and asks it for everything at once.

**It serialises what asked to be serialised.** A grain declares
``reentrant``; a non-reentrant one gets a lock and one call at a time.

**It collects what nobody is using.** A sweeper deactivates grains idle
longer than the configured span. Idleness is measured from the last call to
*finish*, not to start: a grain answering a slow call is not idle, and a
runtime that collected it would deactivate underneath its own caller.

**There is no directory and no transport**, so every grain is here and
single activation is trivially true. Distribution goes in front of
:meth:`Runtime.call` as a directory that either dispatches locally, as now,
or forwards. Nothing in the calling code changes when it arrives, which is
the point of addressing by identity.

**There is no persistence and no supervision.** A grain that fails a call
raises to its caller and stays activated; a grain that fails to activate
leaves nothing behind. Restart policies belong to whatever asked, which in
this system is a job that already knows how to resume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import Any, Self

from nigrains.errors import GrainNotRegisteredError, NoSuchGrainMethodError
from nigrains.grain import Grain, GrainId

log = logging.getLogger(__name__)
"""Named after the module, with no handler and no level.

A library that configures logging decides for its host; this one only
emits. Messages are %-style because that is what the standard library
takes, and structured logging is the application's choice to make.
"""

GrainFactory = Callable[[GrainId], Grain]
"""How a registered kind is built. Takes the identity, returns the behaviour."""


class _Activation:
    """One live grain, and what the runtime needs to know about it.

    Attributes:
        grain: The behaviour.
        ready: Completed once activate() has returned; every caller waits
            on it, so exactly one activation runs however many arrive.
        lock: Held for the duration of a call when the grain is not
            reentrant; None when it is.
        in_flight: Calls currently running, so an idle sweep can tell a
            slow grain from an unused one.
        last_used: When the most recent call finished.
    """

    __slots__ = ("grain", "in_flight", "last_used", "lock", "ready")

    def __init__(self, grain: Grain, *, now: float) -> None:
        """Prepares an activation that has not run its hook yet.

        Args:
            grain: The behaviour to activate.
            now: The current time, from the runtime's clock.
        """
        self.grain = grain
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.lock = None if grain.reentrant else asyncio.Lock()
        self.in_flight = 0
        self.last_used = now


class Runtime:
    """Holds the activations on this node and dispatches calls to them.

    Attributes:
        _factories: Registered kinds, by type name.
        _activations: What is currently activated.
        _idle_seconds: How long a grain may go uncalled before collection.
        _sweep_seconds: How often to look.
        _clock: Where the time comes from, so a test need not spend any.
    """

    def __init__(
        self,
        *,
        idle_seconds: float = 300.0,
        sweep_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Builds an empty runtime.

        Args:
            idle_seconds: A grain uncalled for this long is deactivated.
            sweep_seconds: Interval between collections.
            clock: Monotonic source of the current time.
        """
        self._factories: dict[str, GrainFactory] = {}
        self._activations: dict[GrainId, _Activation] = {}
        self._idle_seconds = idle_seconds
        self._sweep_seconds = sweep_seconds
        self._clock = clock
        self._sweeper: asyncio.Task[None] | None = None

    def register(self, grain_type: str, factory: GrainFactory) -> None:
        """Teaches the runtime how to build one kind of grain.

        Args:
            grain_type: The name identities of this kind carry.
            factory: Builds the behaviour from an identity.

        Raises:
            ValueError: That name is already registered. Silently replacing
                it would leave activations of the old kind answering calls
                meant for the new one.
        """
        if grain_type in self._factories:
            raise ValueError(f"grain type {grain_type!r} is already registered")
        self._factories[grain_type] = factory

    async def call(
        self,
        grain_id: GrainId,
        method: str,
        /,
        *args: Any,  # noqa: ANN401 - an RPC signature; see the docstring
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Invokes one method of one grain, activating it if need be.

        **The signature is an RPC's and the typing stops here**, which is
        deliberate rather than neglected: a call that will one day cross a
        node boundary cannot carry a closure, so it carries a name and
        arguments. Types are restored by whoever writes a reference class
        for their own grain, in their own package, where the methods are
        known.

        Args:
            grain_id: Which grain.
            method: Which of its methods.
            *args: Positional arguments for it.
            **kwargs: Keyword arguments for it.

        Returns:
            Whatever the method returned.

        Raises:
            GrainNotRegisteredError: No factory for that type.
            NoSuchGrainMethodError: The grain has no such method, or the
                name resolves to something that is not callable.
        """
        activation = await self._activated(grain_id)
        target = getattr(activation.grain, method, None)
        if target is None or not callable(target) or method.startswith("_"):
            raise NoSuchGrainMethodError(str(grain_id), method)

        with self._counted(activation):
            if activation.lock is None:
                return await target(*args, **kwargs)
            async with activation.lock:
                return await target(*args, **kwargs)

    async def _activated(self, grain_id: GrainId) -> _Activation:
        """Returns the activation for an identity, building one if needed.

        Args:
            grain_id: Which grain.

        Returns:
            The live activation.

        Raises:
            GrainNotRegisteredError: No factory for that type.
        """
        existing = self._activations.get(grain_id)
        if existing is not None:
            await existing.ready
            return existing

        factory = self._factories.get(grain_id.type)
        if factory is None:
            raise GrainNotRegisteredError(grain_id.type)

        activation = _Activation(factory(grain_id), now=self._clock())
        # Published before the hook runs, so a second caller finds it and
        # waits rather than building a second grain. There is no await
        # between the lookup above and this line, which is what makes that
        # safe on one event loop.
        self._activations[grain_id] = activation
        try:
            await activation.grain.activate()
        except BaseException as exc:
            # Nothing half-built is left behind: the entry goes, and the
            # next call starts over. Every caller waiting sees the failure.
            self._activations.pop(grain_id, None)
            activation.ready.set_exception(exc)
            # Somebody has to consume it if every waiter went away.
            activation.ready.exception()
            raise
        activation.ready.set_result(None)
        log.debug("activated %s", grain_id)
        return activation

    @contextmanager
    def _counted(self, activation: _Activation) -> Iterator[None]:
        """Marks a call as running, so the sweeper leaves the grain alone.

        Args:
            activation: The activation serving the call.

        Yields:
            Nothing; the call runs inside.
        """
        activation.in_flight += 1
        try:
            yield
        finally:
            activation.in_flight -= 1
            activation.last_used = self._clock()

    async def collect(self) -> int:
        """Deactivates every grain that is idle past the configured span.

        Returns:
            How many were collected.
        """
        cutoff = self._clock() - self._idle_seconds
        stale = [
            grain_id
            for grain_id, activation in self._activations.items()
            if activation.in_flight == 0 and activation.last_used <= cutoff
        ]
        for grain_id in stale:
            await self._deactivate(grain_id)
        return len(stale)

    async def _deactivate(self, grain_id: GrainId) -> None:
        """Removes one activation, letting it release what it held.

        Args:
            grain_id: Which grain.
        """
        activation = self._activations.pop(grain_id, None)
        if activation is None:
            return
        try:
            await activation.grain.deactivate()
        except Exception:
            # The activation is going regardless; a grain that cannot tidy
            # up must not be able to keep itself alive by failing.
            log.exception("deactivating %s failed", grain_id)
        log.debug("deactivated %s", grain_id)

    @property
    def activated(self) -> int:
        """How many grains are activated right now.

        Returns:
            The count.
        """
        return len(self._activations)

    async def __aenter__(self) -> Self:
        """Starts the sweeper.

        Returns:
            This runtime.
        """
        self._sweeper = asyncio.create_task(self._sweep())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stops the sweeper and deactivates everything still activated.

        Args:
            exc_type: Exception class, if the block raised.
            exc: The exception, if the block raised.
            traceback: Its traceback, if the block raised.
        """
        if self._sweeper is not None:
            self._sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for grain_id in list(self._activations):
            await self._deactivate(grain_id)

    async def _sweep(self) -> None:
        """Collects idle grains until cancelled."""
        while True:
            await asyncio.sleep(self._sweep_seconds)
            try:
                collected = await self.collect()
            except Exception:
                # A sweeper that dies leaks every grain from then on, and
                # nothing would say so until memory ran out.
                log.exception("the idle sweep failed")
            else:
                if collected:
                    log.debug("collected %d idle grains", collected)
