"""What a grain is, and the two things a grain declares about itself.

A grain is a virtual actor: it has an identity rather than a lifetime, and
the runtime decides when an activation of it exists. Application code never
creates one and never destroys one - it calls one, and the call is what
brings it into being.

**Nothing here knows what a grain is for.** This package has no
dependencies at all, which is what lets it be used by something other than
the system it was written for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nigrains.reminders import GrainReminders
    from nigrains.state import GrainState


@dataclass(frozen=True, slots=True)
class GrainId:
    """Which grain, of which kind.

    The identity is the address. There is no separate handle, no creation
    step and nothing to look up before calling: a grain of a given type and
    key is always callable, and whether an activation of it exists at this
    moment is the runtime's business and not the caller's.

    Attributes:
        type: The registered kind, naming the behaviour.
        key: Which one of that kind. A string because it has to survive a
            wire eventually, and because a runtime that accepted arbitrary
            objects here would have to decide how two of them compare.
    """

    type: str
    key: str

    def __str__(self) -> str:
        """Renders the identity the way logs and directory keys spell it.

        Returns:
            ``type/key``.
        """
        return f"{self.type}/{self.key}"


class Grain:
    """Base class for a grain's behaviour.

    Subclasses add methods; those methods are what a caller invokes by name.
    Two hooks bracket an activation's life, and both have a default that
    does nothing, because most grains need neither.

    **Reentrancy is declared here and nowhere else.** The virtual actor
    model's default is one message at a time, which is what makes a grain's
    state safe without a lock anywhere in the application. That default is
    wrong for a grain whose methods only read: serialising pure reads would
    queue every concurrent caller behind one, which is the opposite of why
    the fleet exists. So a grain says which it is, and says it as a property
    of the class, where it is read once rather than argued about per call.

    A reentrant grain **must not** mutate state in a way another call could
    observe half-done. That is not checked and cannot be; it is the whole
    of what the flag means, and a grain that breaks it has no protection.

    Attributes:
        id: This activation's identity.
        grain_type: The kind this class answers for - the first half of
            every identity addressed to it, and a **wire identifier**: it
            travels in a message and, once there is a cluster, between
            machines. Declared rather than derived from the class name,
            because renaming a class is a refactor and renaming an address
            is a migration, and a default would make them the same
            keystroke.
        reentrant: Whether calls may overlap. Default False, matching the
            model rather than matching our commonest grain.
        tolerates_double_activation: Whether two activations of this
            identity, in two processes at once, are harmless. **Default
            False, which is the safe answer and the wrong one for a cache.**

            A cluster cannot promise otherwise: two halves of a partition
            each believe they own a share and each is right about its own
            half. A grain fronting something immutable is unaffected - two
            of it are two caches answering the same - and says so. A grain
            whose state is the truth must not, and a runtime asked to
            cluster one will refuse rather than let the first split find
            out.

            Read today by the conformance kit, which skips the scenario
            comparing two activations for a grain that never claimed to
            survive them.
        persistent: Whether this grain keeps state in the runtime's store.
            Default False, and the default costs nothing: only a grain that
            says otherwise is read before it activates, so a fleet of caches
            never pays for a store it does not use.

            A grain that says True and is registered with a runtime that has
            no store is refused at registration, because the alternative is
            discovering it on the first activation of the first grain in
            production.
        activations_per_key: How many activations answer for one key, or 0 -
            the default - for the model's own rule of one.

            **The one place this package lets identity stop meaning one
            activation**, and it is for work that has no state to protect:
            something CPU-bound, or a fan-out where what matters is how many
            can run rather than which one does. A pool of eight answers
            eight calls at once where a serialised grain answers one, and a
            reentrant grain answers eight on one event loop, which is not
            the same thing at all when the work is not waiting on anything.

            The wrong shape for everything else. A grain with state and a
            pool is eight copies of that state, disagreeing.

            **Orleans calls this a stateless worker, and this package does
            not**, on purpose. A grain of this kind is still a grain - a
            kilobyte, built on demand, collected when idle - and "worker"
            names the heavy thing the model exists to replace: a process
            that consumes a queue. In a system where both exist, the process
            is the *host* and the grains live inside it, and one word for
            both makes that sentence unreadable.

            **And it sits against the grain of the model**, which is worth
            saying rather than hiding: this package is about addressing one
            thing by its identity, and this is the kind where identity is
            admitted not to matter. Reach for it only when the work has no
            natural key. If it has one, use the key - many grains is what
            this is good at, and a pool is what it falls back to.
    """

    grain_type: ClassVar[str] = ""
    reentrant: ClassVar[bool] = False
    tolerates_double_activation: ClassVar[bool] = False
    activations_per_key: ClassVar[int] = 0
    persistent: ClassVar[bool] = False

    @property
    def state(self) -> GrainState:
        """This grain's own stored state.

        Attached by the runtime before ``activate`` runs, so a grain reads
        what it had without knowing where it was kept.

        Returns:
            The handle.

        Raises:
            RuntimeError: This grain did not declare ``persistent``, or the
                runtime holding it has no store. Both are configuration
                mistakes rather than runtime conditions, and both say so
                here rather than raising AttributeError somewhere further
                on.
        """
        if self._state is None:
            raise RuntimeError(
                f"{type(self).__name__} has no state: it declares "
                f"persistent = {self.persistent!r} and its runtime "
                f"{'has no store' if self.persistent else 'was never asked for one'}"
            )
        return self._state

    @property
    def reminders(self) -> GrainReminders:
        """This grain's own schedule, the part that outlives an activation.

        Returns:
            The handle.

        Raises:
            RuntimeError: The runtime holding this grain has no reminder
                store. A configuration mistake, said here rather than as an
                AttributeError further on.
        """
        if self._reminders is None:
            raise RuntimeError(
                f"{type(self).__name__} asked for reminders and its runtime has no store for them"
            )
        return self._reminders

    async def on_reminder(self, name: str) -> None:
        """Serves a reminder that has come due.

        Overridden by any grain that schedules one. The default does
        nothing rather than raising, because a schedule outlives the code
        that made it: a reminder written by a version that had a handler
        can come due on a version that does not, and taking the process
        down over it would be the wrong end of that trade.

        Args:
            name: What the reminder was called when it was scheduled.
        """

    def __init__(self, grain_id: GrainId) -> None:
        """Binds the activation to its identity.

        Args:
            grain_id: Which grain this is an activation of.
        """
        self.id = grain_id
        self._timers: list[tuple[float, Callable[[], Awaitable[None]]]] = []
        self._state: GrainState | None = None
        self._reminders: GrainReminders | None = None

    def every(self, seconds: float, work: Callable[[], Awaitable[None]]) -> None:
        """Runs something on a schedule for as long as this activation lives.

        Asked for from :meth:`activate`, which is the only place it makes
        sense: the runtime starts the timers once activation has succeeded,
        and stops them when the activation goes.

            async def activate(self) -> None:
                self.prices = await self.load()
                self.every(60.0, self.refresh)

        **A timer does not keep its grain alive.** Ticking is not being
        used: a grain nobody calls is collected on the usual schedule and
        its timers stop with it. That is the whole difference between a
        timer and a reminder, and a timer that prevented collection would
        turn one call into a grain that lives for ever.

        **A tick already running does delay deactivation**, for the same
        reason a call does: finishing under a ``deactivate()`` that has
        already released what the tick is using is the failure the in-flight
        count exists to prevent.

        A tick that raises is logged and the schedule continues. A timer
        that died silently would be worse, and one that took its grain down
        would be worse still.

        Args:
            seconds: How long between ticks. The first tick is one interval
                after activation, not immediately - a grain that wants
                something done at once does it in ``activate``.
            work: What to run. Takes nothing: it is a method of the grain,
                which already has everything.
        """
        self._timers.append((seconds, work))

    async def activate(self) -> None:
        """Prepares the activation before its first call is served.

        Called once, with every caller of this grain waiting on it. Whatever
        a grain needs in order to answer - an index read from a row store, a
        connection, a warmed cache - is read here. A failure propagates to
        every waiting caller and leaves nothing activated, so the next call
        tries again rather than meeting a half-built grain.
        """

    async def deactivate(self) -> None:
        """Releases whatever the activation held.

        Called when the runtime collects an idle grain, and on shutdown.
        **Not a save hook**: this package has no persistence concept, and a
        grain whose state has to survive is a grain that wrote it somewhere
        already. A failure here is logged and does not stop the collection -
        the activation is going away either way.
        """
