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
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from nigrains.errors import GrainNotRegisteredError, NoSuchGrainMethodError
from nigrains.grain import Grain, GrainId
from nigrains.reference import G, reference_to

log = logging.getLogger(__name__)
"""Named after the module, with no handler and no level.

A library that configures logging decides for its host; this one only
emits. Messages are %-style because that is what the standard library
takes, and structured logging is the application's choice to make.
"""

GrainFactory = Callable[[GrainId], Grain]
"""How a registered kind is built. Takes the identity, returns the behaviour."""

_DRAIN_POLL = 0.05
"""Seconds between checks while waiting for calls in flight to finish.

Short enough that a fast shutdown is not made slow by the polling, long
enough that a slow one does not spin."""

_SWEEP_CHUNK = 1_000
"""Activations collected between yields to the event loop.

Small enough that a sweep of a very large fleet never holds the loop for
long, large enough that the yields themselves are not the cost. Not
configurable: it trades one invisible property against another, and a knob
here would be a knob nobody could set from evidence.
"""


class _Activation:
    """One live grain, and what the runtime needs to know about it.

    **Two of these fields are dropped or never made once they stop being
    needed**, because a fleet is meant to be large and this record is paid
    for once per grain. Measuring 100 000 trivial activations put the
    runtime's own bookkeeping at 1228 bytes each - 117 MiB before a single
    grain held anything of its own - and most of that was a future kept
    forever after it had been resolved once, and a lock made eagerly for
    every grain whether two callers ever met on it or not.

    Attributes:
        grain: The behaviour.
        ready: Set once activate() has finished, one way or the other. Every
            caller arriving during activation waits on it, and it is dropped
            the moment a *successful* activation is done, because a warm
            grain has no use for it.

            **An Event and not a Future, which is a fix and not a
            preference.** A future is one object that every waiter awaits
            directly, so cancelling any one waiting task cancels the future
            itself - and with it the activation everybody else was waiting
            for, plus an InvalidStateError in the coroutine that goes on to
            resolve it. An asyncio.timeout around a call to a cold grain is
            enough to cause it. Event.wait gives each waiter a future of its
            own, so one caller giving up is one caller giving up. Found in
            review, demonstrated rather than argued.
        failure: What activate() raised, for waiters to read after the event
            is set. It lives here because an Event, unlike a future, has
            nowhere to carry it.
        lock: Held for the duration of a call when the grain is not
            reentrant. None both when the grain is reentrant and when it is
            not but has not yet been called - made on first use, which on
            one event loop is safe because the check and the assignment
            have no await between them.
        in_flight: Calls currently running, so an idle sweep can tell a
            slow grain from an unused one.
        last_used: When the most recent call finished.
    """

    __slots__ = ("failure", "grain", "in_flight", "last_used", "lock", "ready")

    def __init__(self, grain: Grain, *, now: float) -> None:
        """Prepares an activation that has not run its hook yet.

        Args:
            grain: The behaviour to activate.
            now: The current time, from the runtime's clock.
        """
        self.grain = grain
        self.ready: asyncio.Event | None = asyncio.Event()
        self.failure: BaseException | None = None
        self.lock: asyncio.Lock | None = None
        self.in_flight = 0
        self.last_used = now


@dataclass(frozen=True, slots=True)
class Stats:
    """What the runtime has been doing, as numbers a host can report.

    Read from :attr:`Runtime.stats`. Every field is a plain count, so
    feeding them to whatever the host already uses - a metrics library, a
    log line, a health endpoint - costs nothing and commits this package to
    no dependency and no opinion about how metrics should look.

    Attributes:
        activated: Grains activated right now.
        activations: Activations since the runtime was built, including the
            ones that have since been collected. Rising steadily against a
            flat ``activated`` means grains are being collected and rebuilt,
            which is an idle span set too short.
        failed_activations: Times ``activate()`` raised. Distinct from a
            call failing: nothing was built, and the next call will try
            again.
        deactivations: Grains collected or released on shutdown.
        evictions: Grains deactivated to stay under ``max_activations``.
            Nonzero means the bound is doing something, which is worth
            knowing before blaming the latency on something else.
        calls: Calls dispatched since the runtime was built.
        in_flight: Calls running right now.
    """

    activated: int
    activations: int
    failed_activations: int
    deactivations: int
    evictions: int
    calls: int
    in_flight: int


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
        drain_seconds: float = 30.0,
        max_activations: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Builds an empty runtime.

        Args:
            idle_seconds: A grain uncalled for this long is deactivated.
            sweep_seconds: Interval between collections.
            drain_seconds: How long leaving the runtime waits for calls in
                flight before deactivating anyway. Zero deactivates at once,
                which is what a process being killed wants and what a
                process shutting down does not.
            max_activations: Most grains to keep activated at once, or None
                for no bound. With a bound, activating one more collects the
                least recently used idle grain first - which is what makes
                this a cache rather than a set that only grows. The
                bookkeeping for one grain is about a kilobyte before the
                grain holds anything of its own, so a hundred thousand is
                about a hundred megabytes of runtime and whatever the grains
                are worth on top.
            clock: Monotonic source of the current time.
        """
        self._factories: dict[str, GrainFactory] = {}
        self._activations: OrderedDict[GrainId, _Activation] = OrderedDict()
        self._idle_seconds = idle_seconds
        self._sweep_seconds = sweep_seconds
        self._drain_seconds = drain_seconds
        self._max_activations = max_activations
        self._clock = clock
        self._sweeper: asyncio.Task[None] | None = None
        self._activations_total = 0
        self._failed_activations = 0
        self._deactivations = 0
        self._evictions = 0
        self._calls = 0
        self._in_flight = 0

    def register(self, grain: type[Grain], factory: GrainFactory | None = None) -> None:
        """Teaches the runtime how to build one kind of grain.

        **The class is the argument and the name comes off it**, because a
        name given here and a name looked up there are two sources of truth
        for one address, and a typed reference has to be able to find the
        second from the first.

        Args:
            grain: The grain class. Its ``grain_type`` is the name identities
                of this kind carry.
            factory: Builds the behaviour from an identity. Defaults to the
                class itself, which is right whenever the constructor wants
                nothing but the identity.

        Raises:
            ValueError: The class declares no ``grain_type``, or that name
                is already registered. Silently replacing a registration
                would leave activations of the old kind answering calls
                meant for the new one.
        """
        grain_type = grain.grain_type
        if not grain_type:
            raise ValueError(f"{grain.__name__} declares no grain_type, so it has no address")
        if grain_type in self._factories:
            raise ValueError(f"grain type {grain_type!r} is already registered")
        self._factories[grain_type] = factory if factory is not None else grain

    def reference(self, grain: type[G], key: str) -> G:
        """Returns a reference to one grain, typed as that grain.

        The way to call a grain when the caller knows what kind it is, which
        is nearly always. :meth:`call` remains for the cases that do not -
        a transport forwarding a message it did not compose.

        Args:
            grain: The grain class, supplying the address and the signatures.
            key: Which grain of that kind.

        Returns:
            Something answering the grain's coroutine methods, typed as the
            grain. See :mod:`nigrains.reference` for what that annotation
            does and does not promise.
        """
        return reference_to(self, grain, key)

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
            if activation.grain.reentrant:
                return await target(*args, **kwargs)
            if activation.lock is None:
                activation.lock = asyncio.Lock()
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
        while True:
            existing = self._activations.get(grain_id)
            if existing is None:
                break
            # Read once: a successful activation drops it the instant it is
            # done, and a caller already waiting holds a future of its own.
            pending = existing.ready
            if pending is None:
                return existing
            await pending.wait()
            if existing.failure is None:
                return existing
            if isinstance(existing.failure, asyncio.CancelledError):
                # Somebody else's cancellation, not this caller's. The entry
                # is already gone, so going round builds a fresh activation
                # rather than handing back a cancellation nobody asked for.
                # This loop cannot spin: every turn of it awaits.
                continue
            raise existing.failure

        factory = self._factories.get(grain_id.type)
        if factory is None:
            raise GrainNotRegisteredError(grain_id.type)

        await self._make_room()
        activation = _Activation(factory(grain_id), now=self._clock())
        # Published before the hook runs, so a second caller finds it and
        # waits rather than building a second grain. There is no await
        # between the lookup above and this line, which is what makes that
        # safe on one event loop.
        self._activations[grain_id] = activation
        self._activations_total += 1
        try:
            await activation.grain.activate()
        except BaseException as exc:
            # Nothing half-built is left behind: the entry goes, and the
            # next call starts over. Waiters see the failure, or retry when
            # what failed was a cancellation.
            self._activations.pop(grain_id, None)
            self._failed_activations += 1
            activation.failure = exc
            if activation.ready is not None:
                activation.ready.set()
            raise
        if activation.ready is not None:
            activation.ready.set()
            activation.ready = None
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
        self._in_flight += 1
        self._calls += 1
        try:
            yield
        finally:
            activation.in_flight -= 1
            self._in_flight -= 1
            activation.last_used = self._clock()
            if self._max_activations is not None:
                # Only when bounded: the move is cheap but not free, and a
                # runtime with no bound has no use for the ordering.
                self._activations.move_to_end(activation.grain.id)

    async def _make_room(self) -> None:
        """Collects the least recently used idle grain if the fleet is full.

        **A full fleet never refuses a call.** The grain being asked for is
        needed now; something not being used is not. So this evicts rather
        than rejects, and when nothing can be evicted - every grain is
        answering - it goes over the bound and says so, because stopping
        work to honour a cache size would be the wrong way round.
        """
        if self._max_activations is None or len(self._activations) < self._max_activations:
            return
        for grain_id, activation in self._activations.items():
            if activation.in_flight == 0 and activation.ready is None:
                await self._deactivate(grain_id)
                self._evictions += 1
                return
        log.warning(
            "fleet is at its bound of %d and every grain is busy; activating anyway",
            self._max_activations,
        )

    async def collect(self) -> int:
        """Deactivates every grain that is idle past the configured span.

        **The walk is chunked and the idleness is checked twice**, and both
        are consequences of a fleet being large. A sweep of 100 000 idle
        grains held the event loop for 79 ms in one block while nothing else
        ran; yielding every few thousand turns that into pauses nobody
        notices. And once the sweep can be interrupted, a grain it listed as
        stale can be called before the sweep reaches it - so what was
        decided at the top of the walk is confirmed at the bottom, rather
        than deactivating a grain somebody is using.

        **A grain that has not finished activating is never idle**, however
        long it has been sitting there. Its call count is still zero and its
        timestamp is the moment it was created, so an ``activate()`` slower
        than the idle span used to look exactly like abandonment - and the
        sweep would deactivate a grain whose own first caller was still
        waiting for it, which is the failure the call count exists to
        prevent, moved one step earlier. Found in review, by running it.

        **One consequence worth knowing, because it is not prevented.** A
        grain collected here while a fresh call is already building a new
        activation of the same identity means two activations of one grain
        exist for a moment - the old one finishing its ``deactivate()``, the
        new one serving. Harmless for a grain that fronts something
        immutable, which is what this model is good at. A grain holding an
        exclusive resource must not assume otherwise.

        Returns:
            How many were collected.
        """
        cutoff = self._clock() - self._idle_seconds
        stale = [
            grain_id
            for grain_id, activation in self._activations.items()
            if activation.ready is None
            and activation.in_flight == 0
            and activation.last_used <= cutoff
        ]
        collected = 0
        for index, grain_id in enumerate(stale):
            activation = self._activations.get(grain_id)
            if (
                activation is None
                or activation.ready is not None
                or activation.in_flight
                or activation.last_used > cutoff
            ):
                continue
            await self._deactivate(grain_id)
            collected += 1
            if index % _SWEEP_CHUNK == _SWEEP_CHUNK - 1:
                await asyncio.sleep(0)
        return collected

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
        self._deactivations += 1
        log.debug("deactivated %s", grain_id)

    @property
    def activated(self) -> int:
        """How many grains are activated right now.

        Returns:
            The count.
        """
        return len(self._activations)

    @property
    def stats(self) -> Stats:
        """What this runtime has been doing.

        Returns:
            A snapshot. Reading it costs nothing and changes nothing, so a
            health endpoint may call it as often as it likes.
        """
        return Stats(
            activated=len(self._activations),
            activations=self._activations_total,
            failed_activations=self._failed_activations,
            deactivations=self._deactivations,
            evictions=self._evictions,
            calls=self._calls,
            in_flight=self._in_flight,
        )

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
        await self._drain()
        for grain_id in list(self._activations):
            await self._deactivate(grain_id)

    async def _drain(self) -> None:
        """Waits for calls in flight, up to the shutdown grace.

        **The sweep already refuses to collect a grain that is answering,
        and leaving the runtime used not to**, which put the same failure at
        the one moment it is most likely: a process stopping in the middle
        of work ran every ``deactivate()`` under a running call. Found by
        reading the two paths next to each other rather than by a test,
        which is why there is now a test.

        The wait is bounded. A grain that never finishes must not be able to
        stop a process from stopping, so after the grace the deactivation
        goes ahead and says so.
        """
        deadline = self._clock() + self._drain_seconds
        while self._clock() < deadline:
            busy = sum(1 for activation in self._activations.values() if activation.in_flight)
            if not busy:
                return
            await asyncio.sleep(_DRAIN_POLL)
        still_busy = sum(1 for activation in self._activations.values() if activation.in_flight)
        if still_busy:
            log.warning("deactivating %d grains with calls still in flight", still_busy)

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
